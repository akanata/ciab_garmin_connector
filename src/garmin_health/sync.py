"""The sync sequence, the background loop, and the status it reports.

This module deliberately imports no ``garmindb``: it drives an :class:`Ingest`
port, so GarminDB can be swapped for the real Garmin API later, and so the whole
sequence is testable with no account and no network. ``garmin/ingest.py`` is the
adapter that actually calls GarminDB.

Two GarminDB behaviours shape the design:

- **Its importers swallow every per-file exception**, so a totally failed sync
  looks successful. Row counts and ``latest_time`` before and after are the only
  evidence that anything happened, which is why every report carries both.
- **A download sleeps a second per day and retries five times with backoff**, so
  a multi-year backfill runs for tens of minutes and cannot be interrupted
  mid-call. The loop is therefore cancellable at the phase boundaries, and a
  cooperative stop flag is passed down so the adapter can bail between stats.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable
from collections.abc import Mapping
from enum import StrEnum
from threading import Event
from threading import Lock
from typing import Any
from typing import Protocol

import anyio.to_thread
import attrs

from garmin_health.auth import GarminAuthenticator
from garmin_health.auth import LinkState
from garmin_health.config import Settings

logger = logging.getLogger(__name__)

# How long /setup may show a cached picture of what each metric holds. Short
# enough that a finished sync shows up on the next refresh, long enough that
# holding the page open does not rebuild an ingest every few seconds.
COVERAGE_TTL_SECONDS = 30.0


class SyncPhase(StrEnum):
    DOWNLOAD = "download"
    IMPORT = "import"
    ANALYZE = "analyze"
    REBUILD = "rebuild"


@attrs.frozen
class TableStat:
    """How much of one table exists, as evidence a sync did something.

    ``latest`` is the ISO form of the stored *naive* value, carried as a string
    because it is a local wall clock on GarminDB's own clock, not an instant.
    Converting it is ``TimeZonePolicy``'s job, not the status endpoint's.
    """

    rows: int
    latest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"rows": self.rows, "latest": self.latest}


ProgressSink = Callable[[str, int, int], None]
"""``(what is happening now, steps finished, steps total)``. ``total`` 0 = unknown."""


def _no_progress(label: str, done: int, total: int) -> None:
    """Default sink, so the port can be driven without an engine attached."""


@attrs.frozen
class StatCoverage:
    """How much history one statistic actually holds, against what was asked for.

    ``earliest``/``latest`` are ISO **dates** of the stored naive values -- they
    are wall clocks on GarminDB's own clock, not instants, and converting them is
    ``TimeZonePolicy``'s job rather than a status endpoint's.
    """

    stat: str
    enabled: bool
    rows: int
    earliest: str | None
    latest: str | None
    floor: str

    @property
    def missing_days(self) -> int:
        """Days between the configured floor and the oldest row we hold.

        Zero for an empty metric: a normal sync already starts at the floor when a
        table has no rows, so a backfill there would just duplicate "Sync now".
        """
        if self.rows == 0 or self.earliest is None:
            return 0
        try:
            earliest = dt.date.fromisoformat(self.earliest[:10])
            floor = dt.date.fromisoformat(self.floor[:10])
        except ValueError:  # pragma: no cover - both are rendered by us
            return 0
        return max((earliest - floor).days, 0)

    @property
    def has_gap(self) -> bool:
        return self.missing_days > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stat": self.stat,
            "enabled": self.enabled,
            "rows": self.rows,
            "earliest": self.earliest,
            "latest": self.latest,
            "floor": self.floor,
            "missing_days": self.missing_days,
        }


@attrs.frozen
class SyncStep:
    """What the worker thread is doing right now."""

    label: str
    done: int = 0
    total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "done": self.done, "total": self.total}


class Ingest(Protocol):
    """The GarminDB-facing port. Every method is synchronous and blocking."""

    def table_stats(self) -> dict[str, TableStat]: ...
    def stat_coverage(self) -> dict[str, StatCoverage]: ...
    def download(self, stop: Event, progress: ProgressSink = ...) -> None: ...
    def backfill(self, stop: Event, progress: ProgressSink = ...) -> None: ...
    def import_(self, stop: Event, progress: ProgressSink = ...) -> None: ...
    def analyze(self) -> None: ...
    def rebuild(self, stop: Event, progress: ProgressSink = ...) -> None: ...


@attrs.frozen
class SyncReport:
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    phase: SyncPhase | None = None
    error: str | None = None
    before: Mapping[str, TableStat] = attrs.field(factory=dict)
    after: Mapping[str, TableStat] = attrs.field(factory=dict)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def row_delta(self) -> dict[str, int]:
        return {
            name: stat.rows - self.before.get(name, TableStat(rows=0)).rows
            for name, stat in self.after.items()
        }

    @property
    def changed(self) -> bool:
        """False is not necessarily a failure -- an up-to-date corpus changes
        nothing -- but combined with a clean error it is the only way to notice
        that every file silently failed to import."""
        return any(delta != 0 for delta in self.row_delta.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": self.duration_seconds,
            "phase": self.phase.value if self.phase else None,
            "error": self.error,
            "changed": self.changed,
            "row_delta": self.row_delta,
            "tables": {name: stat.as_dict() for name, stat in self.after.items()},
        }


def incremental_range(
    *,
    latest: dt.datetime | dt.date | None,
    today: dt.date,
    fallback: tuple[dt.date, int],
) -> tuple[dt.date, int]:
    """GarminDB's own incremental rule, from ``garmindb_cli.py``'s ``__get_date_and_days``.

    Start one day *before* the newest row, because the last download may have
    captured a partial day. With no rows at all, fall back to the configured
    ``<stat>_start_date``. This is what makes routine syncs cheap.
    """
    if latest is None:
        return fallback
    latest_date = latest.date() if isinstance(latest, dt.datetime) else latest
    start = latest_date - dt.timedelta(days=1)
    # Clamped: a clock skew or a future-dated row must not ask Garmin for a
    # negative span, which Download would loop over zero times but report oddly.
    return start, max((today - start).days, 0)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class SyncEngine:
    """Owns the sync lock, the last report, and the interval loop."""

    def __init__(
        self,
        *,
        settings: Settings,
        authenticator: GarminAuthenticator,
        ingest_factory: Callable[[], Ingest],
        clock: Callable[[], dt.datetime] = _utcnow,
        on_corpus_changed: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._auth = authenticator
        self._ingest_factory = ingest_factory
        self._clock = clock
        # Called once after each completed sync, whatever the outcome. The serving
        # layer uses it to drop pooled handles that may now point at a deleted
        # inode, and to retry a timezone that could only be resolved once the
        # profile importers had run. Deliberately a bare callable: sync.py imports
        # no garmindb, and that is what keeps this whole sequence testable.
        self._on_corpus_changed = on_corpus_changed
        self._lock = asyncio.Lock()
        self._stop = Event()
        self._last: SyncReport | None = None
        self._task: asyncio.Task[SyncReport | None] | None = None
        # Written from the worker thread, read from the event loop, so it needs a
        # real lock rather than relying on the GIL for a two-field update.
        self._step_lock = Lock()
        self._step: SyncStep | None = None
        self._coverage: dict[str, StatCoverage] | None = None
        self._coverage_at = 0.0

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    def _report_progress(self, label: str, done: int = 0, total: int = 0) -> None:
        """The sink handed to the ingest port. Called on the worker thread."""
        with self._step_lock:
            self._step = SyncStep(label=label, done=done, total=total)
        logger.info("Sync progress: %s", label)

    def _clear_progress(self) -> None:
        with self._step_lock:
            self._step = None

    @property
    def step(self) -> SyncStep | None:
        with self._step_lock:
            return self._step

    async def coverage(self, *, refresh: bool = False) -> dict[str, StatCoverage]:
        """Per-metric history, behind a short TTL.

        Building an ingest re-renders GarminConnectConfig.json and opens both
        databases, so ``/setup`` must not pay that on every refresh. The cache is
        dropped outright after a sync, which is the only thing that changes the
        answer materially.
        """
        now = time.monotonic()
        if (
            not refresh
            and self._coverage is not None
            and now - self._coverage_at < COVERAGE_TTL_SECONDS
        ):
            return self._coverage
        coverage: dict[str, StatCoverage]
        try:
            coverage = await self._in_thread(self._ingest_factory().stat_coverage)
        except Exception as exc:
            # A stale schema makes this raise, and /setup still has to render --
            # it is where the owner goes to press Rebuild.
            logger.warning("Could not read per-metric coverage: %s", exc)
            coverage = {}
        self._coverage = coverage
        self._coverage_at = now
        return coverage

    def request_stop(self) -> None:
        """Ask an in-flight sync to stop at its next phase or stat boundary."""
        self._stop.set()

    def status(self) -> dict[str, Any]:
        step = self.step
        return {
            "link_state": self._auth.status().state.value,
            "running": self.is_running,
            "interval_seconds": self._settings.sync_interval_seconds,
            "progress": step.as_dict() if step else None,
            "last_sync": self._last.as_dict() if self._last else None,
        }

    async def trigger(self, *, backfill: bool = False) -> bool:
        """Start a sync in the background. False if one is already in flight."""
        if self.is_running:
            return False
        self._task = asyncio.create_task(self.run_once(backfill=backfill))
        self._task.add_done_callback(self._log_task_result)
        # Let the task reach its first await so is_running is true on return,
        # which is what makes a second POST /sync see the in-flight run.
        await asyncio.sleep(0)
        return True

    async def trigger_rebuild(self) -> bool:
        """Start a rebuild in the background. False if anything is already running."""
        if self.is_running:
            return False
        self._task = asyncio.create_task(self.rebuild_once())
        self._task.add_done_callback(self._log_task_result)
        await asyncio.sleep(0)
        return True

    async def wait_for_idle(self) -> None:
        """Test and shutdown helper: await the backgrounded run, if any."""
        if self._task is not None:
            await asyncio.shield(self._task)

    @staticmethod
    def _log_task_result(task: asyncio.Task[SyncReport | None]) -> None:
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:  # pragma: no cover - run_once catches its own
            logger.error("Background sync task failed: %s", exception)

    async def run_once(self, *, backfill: bool = False) -> SyncReport | None:
        """Run one download -> import -> analyze cycle.

        With ``backfill=True`` the download phase fetches the *older* range each
        metric is missing against the configured floor, instead of the newer range
        it is missing against today. Everything after that is identical, because
        the importers only care that files landed on disk.

        Returns ``None`` if the account is not linked or a sync is already running.
        Phase failures are recorded on the report rather than raised: the caller is
        a background loop or a fire-and-forget endpoint, and neither can do
        anything useful with an exception.
        """
        if self._auth.status().state is not LinkState.LINKED:
            logger.info("Skipping sync: the Garmin account is not linked.")
            return None
        if self.is_running:
            logger.info("Skipping sync: one is already in flight.")
            return None

        async with self._lock:
            self._stop.clear()
            ingest = self._ingest_factory()
            started = self._clock()
            phase: SyncPhase | None = None
            error: str | None = None
            before: dict[str, TableStat] = {}
            after: dict[str, TableStat] = {}

            try:
                before = await self._in_thread(ingest.table_stats)
                phase = SyncPhase.DOWNLOAD
                # A phase label of our own before handing off, so the page says
                # something the instant a sync starts rather than staying blank
                # until the adapter happens to reach its first reportable step.
                self._report_progress(
                    "Fetching older history from Garmin"
                    if backfill
                    else "Fetching new data from Garmin"
                )
                fetch = ingest.backfill if backfill else ingest.download
                await self._in_thread(fetch, self._stop, self._report_progress)
                phase = SyncPhase.IMPORT
                self._report_progress("Importing downloaded files")
                await self._in_thread(ingest.import_, self._stop, self._report_progress)
                phase = SyncPhase.ANALYZE
                self._report_progress("Building summaries")
                await self._in_thread(ingest.analyze)
                phase = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("Sync failed during %s", phase)
            finally:
                # A progress line left standing reads as a sync that never
                # finished, which is worse than no progress line at all.
                self._clear_progress()

            try:
                after = await self._in_thread(ingest.table_stats)
            except Exception as exc:
                logger.warning("Could not read table stats after the sync: %s", exc)
                after = dict(before)

            report = SyncReport(
                started_at=started,
                finished_at=self._clock(),
                phase=phase,
                error=error,
                before=before,
                after=after,
            )
            self._last = report
            self._notify_corpus_changed()
            if error is None and not report.changed:
                logger.warning(
                    "Sync reported success but no table grew. GarminDB's importers swallow "
                    "per-file errors, so verify the corpus if this repeats: %s",
                    report.row_delta,
                )
            return report

    async def rebuild_once(self) -> SyncReport | None:
        """Rebuild the local databases from the retained JSON/FIT corpus.

        This is the owner's way out of a schema mismatch, which is otherwise a dead
        end in a container: the DB files are the problem, and nothing else can
        delete them. Deliberately **no download phase** -- persisting the raw
        corpus is exactly what makes a rebuild a local reimport of minutes rather
        than hours of re-downloading.

        It also needs no linked account, because it touches nothing but local
        files. Refusing while unlinked would strand a container whose corpus is
        broken and whose token has since expired.
        """
        if self.is_running:
            logger.info("Skipping rebuild: a sync or rebuild is already in flight.")
            return None

        async with self._lock:
            self._stop.clear()
            started = self._clock()
            phase: SyncPhase | None = SyncPhase.REBUILD
            error: str | None = None
            before: dict[str, TableStat] = {}
            after: dict[str, TableStat] = {}

            try:
                ingest = self._ingest_factory()
                before = await self._in_thread(ingest.table_stats)
            except Exception as exc:
                # Expected when the schema is broken -- which is the whole reason
                # to be here -- so it must not stop the rebuild.
                logger.info("Could not read table stats before the rebuild: %s", exc)
                ingest = self._ingest_factory()

            try:
                self._report_progress("Rebuilding the local databases")
                await self._in_thread(ingest.rebuild, self._stop, self._report_progress)
                phase = None
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("Rebuild failed")
            finally:
                self._clear_progress()

            try:
                after = await self._in_thread(ingest.table_stats)
            except Exception as exc:
                logger.warning("Could not read table stats after the rebuild: %s", exc)
                after = dict(before)

            report = SyncReport(
                started_at=started,
                finished_at=self._clock(),
                phase=phase,
                error=error,
                before=before,
                after=after,
            )
            self._last = report
            self._notify_corpus_changed()
            return report

    def _notify_corpus_changed(self) -> None:
        """A failing callback must never turn a good sync into a failed one."""
        self._coverage = None
        if self._on_corpus_changed is None:
            return
        try:
            self._on_corpus_changed()
        except Exception:
            logger.exception("The post-sync corpus callback failed; continuing.")

    @staticmethod
    async def _in_thread(func: Callable[..., Any], *args: Any) -> Any:
        # abandon_on_cancel: a download blocks in time.sleep for minutes and cannot
        # be interrupted, so shutdown must not wait for it. The process is going
        # away; GarminDB commits per file, and a partial corpus is re-importable
        # from the retained JSON/FIT files without re-downloading.
        return await anyio.to_thread.run_sync(func, *args, abandon_on_cancel=True)

    async def run_forever(self) -> None:
        """Sleep, sync, repeat. Cancelled by the app lifespan.

        Sleeping first is deliberate: a crash-looping container would otherwise
        replay a Garmin sign-in on every restart.
        """
        logger.info("Sync loop started; interval %ss.", self._settings.sync_interval_seconds)
        while True:
            await asyncio.sleep(self._settings.sync_interval_seconds)
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # run_once records its own failures; this is belt and braces so a
                # bug in the reporting path cannot kill the loop for good.
                logger.exception("Unexpected error in the sync loop; continuing.")
