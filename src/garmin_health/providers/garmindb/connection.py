"""The gateway to the GarminDB corpus: DB handles, sessions, and the tz policy.

Two facts about ``idbutils`` force this design.

**Constructing a DB object is a write.** ``DB.__init__`` runs ``create_all()`` and
a ``version_check()`` that inserts ``_attributes`` rows, so ``DBs/`` can never be
mounted read-only -- and, usefully, so a first boot against an empty directory
produces a complete valid schema rather than an error.

**Table classmethods do not work until a DB has been constructed.**
``DbObject.setup()`` -- which installs ``cls.time_col`` and ``cls.col_names`` -- is
only called from ``DB.init_table()`` inside ``DB.__init__``. Without a constructed
``MonitoringDb``, ``MonitoringHeartRate.s_get_for_period(...)`` raises
``AttributeError``, so both DB objects are built once at startup and kept.

Nothing here crashes the process. A stale schema, an unresolved timezone and a
corpus that has never been synced are all *states*, and each is recorded so that
``/health`` stays 200, ``/setup`` stays reachable, and only ``/v1/*`` degrades.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any
from typing import TypeVar

from garmindb.garmindb import GarminDb
from garmindb.garmindb import MonitoringDb
from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import Sleep
from idbutils import DbParams
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from garmin_health.config import Settings
from garmin_health.errors import ProviderUnavailable
from garmin_health.providers.garmindb.timezone_probe import resolve_policy
from garmin_health.timezones import TimeZonePolicy
from garmin_health.timezones import TimeZoneUnresolved

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The sync engine writes these same files while requests are being served. Without
# a busy timeout SQLite raises "database is locked" the instant it meets a writer's
# lock; with one, a reader simply waits out the commit.
BUSY_TIMEOUT_MS = 5_000


class GarminUnavailable(ProviderUnavailable):
    """The corpus cannot be served right now. Routes turn this into a 503.

    A :class:`ProviderUnavailable` so the HTTP layer maps it without importing
    this package: the status is the contract, GarminDB is an implementation
    detail of why.
    """


class GarminSchemaMismatch(GarminUnavailable):
    """The on-disk GarminDB schema is not the version this GarminDB writes."""


def _register_busy_timeout(engine: Any) -> None:
    @event.listens_for(engine, "connect")
    def _set_busy_timeout(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()


class GarminConnection:
    """Owns the two DB handles and the timezone policy for one corpus.

    Constructed once at startup and explicitly managed thereafter: there is no
    crash-and-restart path anywhere in this class.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Guards the handle swap in reset(). It is NOT held for the duration of a
        # read: dispose() detaches in-use connections rather than closing them, so
        # an in-flight session finishes against the old engine safely, and holding
        # the lock across a query would serialize every reader behind every other.
        self._lock = threading.RLock()
        self._garmin_db: GarminDb | None = None
        self._monitoring_db: MonitoringDb | None = None
        self._tz: TimeZonePolicy | None = None
        self._tz_error: TimeZoneUnresolved | None = None
        self._fault: str | None = None
        self._open()

    # -- lifecycle ------------------------------------------------------------

    def _open(self) -> None:
        """Build both DB objects and resolve the timezone policy.

        ``get_db_dir()`` mkdirs as a side effect in GarminDB; we do it explicitly
        because the serving side never loads GarminConnectConfig.json.
        """
        self._settings.db_dir.mkdir(parents=True, exist_ok=True)
        params = DbParams(db_type="sqlite", db_path=str(self._settings.db_dir))
        try:
            garmin_db = GarminDb(params)
            monitoring_db = MonitoringDb(params)
        except RuntimeError as exc:
            # idbutils raises a bare RuntimeError from its version check. Starting
            # degraded beats refusing to start: the router would restart-loop a
            # container whose only problem is a schema the owner can rebuild.
            self._fault = (
                f"The GarminDB schema on disk is not the version this GarminDB writes, so it "
                f"must be rebuilt before health data can be served again ({exc})."
            )
            logger.error("GarminDB schema mismatch; /v1/* will report unavailable. %s", exc)
            return

        _register_busy_timeout(garmin_db.engine)
        _register_busy_timeout(monitoring_db.engine)
        self._garmin_db = garmin_db
        self._monitoring_db = monitoring_db
        self._fault = None
        self._resolve_tz()

    def _resolve_tz(self) -> None:
        """Resolve the policy once, recording rather than raising on failure.

        On a container that has never synced, the account's zone has not been
        imported yet, so this legitimately fails at boot and succeeds later.
        """
        if self._garmin_db is None:
            return
        try:
            self._tz = resolve_policy(
                self._garmin_db,
                configured_home_tz=self._settings.home_tz,
                configured_import_tz=self._settings.import_tz,
            )
            self._tz_error = None
        except TimeZoneUnresolved as exc:
            self._tz = None
            self._tz_error = exc
            logger.warning("Timezone policy unresolved: %s", exc)

    def reset(self) -> None:
        """Dispose both engines and rebuild the handles.

        Required after any DB rebuild: pooled connections would otherwise point at
        the **deleted inode** and go on serving the old rows with no error at all.
        Also the retry path for a transient OperationalError, and the second chance
        for a timezone that could not be resolved at boot.
        """
        with self._lock:
            self._dispose()
            self._open()

    def _dispose(self) -> None:
        for db in (self._garmin_db, self._monitoring_db):
            if db is not None:
                db.engine.dispose()
        self._garmin_db = None
        self._monitoring_db = None

    def close(self) -> None:
        with self._lock:
            self._dispose()

    def __enter__(self) -> GarminConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- state ----------------------------------------------------------------

    @property
    def fault(self) -> str | None:
        """A human-readable reason ``/v1/*`` is unavailable, or None."""
        return self._fault

    @property
    def tz(self) -> TimeZonePolicy:
        """The corpus's timezone policy.

        Raises rather than guessing. There is deliberately no fallback to the
        container's local zone: a silent wrong answer shifts every timestamp this
        service emits and would not be noticed for months.
        """
        if self._tz is None:
            raise self._tz_error or TimeZoneUnresolved(
                "The Garmin account's home timezone is unknown. Set GARMIN_HOME_TZ."
            )
        return self._tz

    @property
    def timezone_error(self) -> TimeZoneUnresolved | None:
        return self._tz_error

    @property
    def settings(self) -> Settings:
        return self._settings

    def status(self) -> dict[str, Any]:
        """What the owner-facing endpoints report about the serving side."""
        return {
            "db_dir": str(self._settings.db_dir),
            "fault": self._fault,
            "timezone": str(self._tz.home_tz) if self._tz else None,
            "timezone_error": str(self._tz_error) if self._tz_error else None,
        }

    # -- reading --------------------------------------------------------------

    @contextmanager
    def sessions(self) -> Iterator[tuple[Session, Session]]:
        """One session per database, since these are two files with two engines.

        ``managed_session()`` is ``sessionmaker(engine, expire_on_commit=False).begin()``,
        so ORM instances stay usable after the block.
        """
        with self._lock:
            garmin_db, monitoring_db = self._garmin_db, self._monitoring_db
        if garmin_db is None or monitoring_db is None:
            if self._fault is not None:
                raise GarminSchemaMismatch(self._fault)
            raise GarminUnavailable("The GarminDB connection is closed.")
        with garmin_db.managed_session() as g, monitoring_db.managed_session() as m:
            yield g, m

    def read(self, fn: Callable[[Session, Session], T]) -> T:
        """Run ``fn(garmin_session, monitoring_session)``, retrying once on a lock.

        Exactly one retry, and only for ``OperationalError``. Retrying forever
        would spin a worker thread against a genuinely broken file until the
        request timed out, with no error ever reaching the consumer.
        """
        try:
            with self.sessions() as (g, m):
                return fn(g, m)
        except OperationalError as exc:
            logger.warning(
                "GarminDB query failed (%s); resetting the connection and retrying.", exc
            )
            self.reset()
            with self.sessions() as (g, m):
                return fn(g, m)

    def has_any_data(self) -> bool:
        """Whether the corpus holds anything at all.

        Only consulted when the timezone is unresolved, to tell a never-synced
        container (serve empty, 200) from a misconfigured one (503). Both tables
        are indexed on their primary key, so this is a cheap existence probe.
        """
        try:
            return self.read(
                lambda g, m: (
                    g.query(Sleep.day).first() is not None
                    or m.query(MonitoringHeartRate.timestamp).first() is not None
                )
            )
        except (GarminUnavailable, OperationalError):
            return False
