"""Drives GarminDB in-process: download -> import -> analyze.

This replicates ``garmindb_cli.py``'s sequence rather than shelling out to it, so
the work happens on a worker thread we control and can report on. The GarminDB
classes are injected as :class:`Bindings` so the sequence is testable without a
Garmin account.

The ordering is not optional: the profile importers must run before anything that
reads ``measurement_system``, and analyze must run last over the imported rows.
"""

from __future__ import annotations

import datetime as dt
import logging
import shutil
from collections.abc import Callable
from threading import Event
from typing import Any

import attrs
import fitfile.units
from garmindb import Analyze
from garmindb import Download
from garmindb import GarminHrvData
from garmindb import GarminHydrationData
from garmindb import GarminMonitoringFitData
from garmindb import GarminPersonalInformation
from garmindb import GarminRhrData
from garmindb import GarminSleepData
from garmindb import GarminSleepFitData
from garmindb import GarminSocialProfile
from garmindb import GarminSummaryData
from garmindb import GarminUserSettings
from garmindb import MonitoringFitFileProcessor
from garmindb import PluginManager
from garmindb import SleepFitFileProcessor
from garmindb.garmindb import Attributes
from garmindb.garmindb import GarminDb
from garmindb.garmindb import Hrv
from garmindb.garmindb import MonitoringDb
from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import RestingHeartRate
from garmindb.garmindb import Sleep
from garmindb.garmindb import SleepEvents

from garmin_health.progress import ProgressSink
from garmin_health.progress import no_progress
from garmin_health.providers.garmindb.config_file import load_manager

# TableStat is a plain value object on the port between sync.py and this adapter;
# importing it here does not drag garmindb into sync.py, which is what keeps the
# engine testable and GarminDB swappable.
from garmin_health.providers.garmindb.preferences import STAT_LABELS
from garmin_health.providers.garmindb.settings import GarminDbSettings
from garmin_health.providers.garmindb.sync import StatCoverage
from garmin_health.providers.garmindb.sync import TableStat
from garmin_health.providers.garmindb.sync import incremental_range
from garmin_health.providers.garmindb.timezone_probe import read_stored_time_zone
from garmin_health.providers.garmindb.timezones import TimeZoneUnresolved
from garmin_health.providers.garmindb.timezones import resolve_home_tz

logger = logging.getLogger(__name__)

# Importers are told latest=True, which means "files whose mtime is in the last
# 24h", NOT "the newest N". That is only safe because the download step has just
# rewritten exactly the files we care about. The cost is that a file which failed
# to import on an earlier run is not retried; row counts in /sync/status are what
# make that visible.
IMPORT_LATEST = True
DEBUG = 0

# Which table stands for each downloadable statistic: the database it lives in,
# the table class, and the column ``latest_time`` filters on to skip placeholder
# rows. Single source of truth for the download plan and the coverage report, so
# the two cannot drift apart and report different histories for the same metric.
STAT_TABLES: dict[str, tuple[str, Any, Any]] = {
    "monitoring": ("monitoring", MonitoringHeartRate, MonitoringHeartRate.heart_rate),
    "sleep": ("garmin", Sleep, Sleep.total_sleep),
    "rhr": ("garmin", RestingHeartRate, RestingHeartRate.resting_heart_rate),
    "hrv": ("garmin", Hrv, Hrv.day),
}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _refuse_mfa() -> str:
    raise RuntimeError(
        "Garmin asked for an MFA code during a background sync. GarminDB's default prompt "
        "reads stdin, which would hang this worker for ever. Re-link the account at /setup."
    )


@attrs.frozen
class Bindings:
    """The GarminDB classes this adapter drives. Overridden wholesale in tests."""

    download: Callable[..., Any] = Download
    user_settings: Callable[..., Any] = GarminUserSettings
    personal_information: Callable[..., Any] = GarminPersonalInformation
    social_profile: Callable[..., Any] = GarminSocialProfile
    summary: Callable[..., Any] = GarminSummaryData
    hydration: Callable[..., Any] = GarminHydrationData
    monitoring_fit: Callable[..., Any] = GarminMonitoringFitData
    monitoring_processor: Callable[..., Any] = MonitoringFitFileProcessor
    sleep_json: Callable[..., Any] = GarminSleepData
    sleep_fit: Callable[..., Any] = GarminSleepFitData
    sleep_processor: Callable[..., Any] = SleepFitFileProcessor
    rhr: Callable[..., Any] = GarminRhrData
    hrv: Callable[..., Any] = GarminHrvData
    analyze: Callable[..., Any] = Analyze


class GarminDbIngest:
    """One sync's worth of GarminDB work. Every method blocks; never call on the loop."""

    def __init__(
        self,
        settings: GarminDbSettings,
        *,
        bindings: Bindings | None = None,
        clock: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._bindings = bindings or Bindings()
        # load_manager validates and repairs the JSON first: the bare
        # GarminConnectConfigManager calls sys.exit(-1) on a malformed config,
        # which in a server process nothing can intercept.
        self._config = load_manager(settings)
        self._db_params = self._config.get_db_params()
        # MonitoringFitFileProcessor.write_file dereferences plugin_manager
        # unconditionally, so None raises AttributeError partway through an import.
        self.plugin_manager = PluginManager(self._config.get_plugins_dir(), self._db_params)
        self._handles: tuple[Any, Any] | None = None

    # -- database handles -----------------------------------------------------

    @property
    def _dbs(self) -> tuple[Any, Any]:
        """The two DB objects, built on first use.

        Constructing a DB runs create_all plus a version check -- it is a write,
        and it is also what installs ``cls.time_col`` on every table class. It is
        deliberately NOT done in ``__init__``: a stale schema raises there, and
        ``rebuild()`` has to be able to exist in order to delete the very files
        that are raising.
        """
        if self._handles is None:
            self._handles = (GarminDb(self._db_params), MonitoringDb(self._db_params))
        return self._handles

    @property
    def _garmin_db(self) -> Any:
        return self._dbs[0]

    @property
    def _monitoring_db(self) -> Any:
        return self._dbs[1]

    # -- evidence -------------------------------------------------------------

    def table_stats(self) -> dict[str, TableStat]:
        """Row counts and newest stored value per table.

        GarminDB's importers swallow every per-file exception, so a totally failed
        sync looks successful. This is the only evidence that anything happened.
        """
        sources: list[tuple[str, Any, Any, Any]] = [
            ("sleep", self._garmin_db, Sleep, Sleep.day),
            ("sleep_events", self._garmin_db, SleepEvents, SleepEvents.timestamp),
            ("resting_hr", self._garmin_db, RestingHeartRate, RestingHeartRate.day),
            ("hrv", self._garmin_db, Hrv, Hrv.day),
            (
                "monitoring_hr",
                self._monitoring_db,
                MonitoringHeartRate,
                MonitoringHeartRate.timestamp,
            ),
        ]
        stats: dict[str, TableStat] = {}
        for name, db, table, column in sources:
            latest = table.latest_time(db, column)
            stats[name] = TableStat(
                rows=table.row_count(db),
                # Deliberately a string: this is a naive local wall clock on
                # GarminDB's own clock, not an instant anyone should convert here.
                latest=latest.isoformat() if latest is not None else None,
            )
        return stats

    def _db_for(self, kind: str) -> Any:
        return self._garmin_db if kind == "garmin" else self._monitoring_db

    def stat_coverage(self) -> dict[str, StatCoverage]:
        """How much history each selectable metric holds, against the owner's floor.

        Reports **every** selectable statistic, including disabled ones: the owner
        needs to see what a metric already holds in order to decide whether to
        switch it back on.

        ``earliest`` is a plain minimum over the time column with no not-zero
        filter, unlike ``latest_time``. A placeholder row still represents a day
        that was downloaded, and this answers "how far back does the corpus go".
        """
        enabled = {stat.name for stat in self._config.enabled_stats()}
        coverage: dict[str, StatCoverage] = {}
        for stat, (kind, table, column) in STAT_TABLES.items():
            db = self._db_for(kind)
            floor, _ = self._config.stat_start_date(stat)
            earliest = table.get_col_min(db, table.time_col)
            latest = table.latest_time(db, column)
            coverage[stat] = StatCoverage(
                stat=stat,
                enabled=stat in enabled,
                rows=table.row_count(db),
                earliest=earliest.date().isoformat() if earliest is not None else None,
                latest=latest.date().isoformat() if latest is not None else None,
                floor=floor.isoformat(),
            )
        return coverage

    # -- download -------------------------------------------------------------

    def _home_today(self) -> dt.date:
        """Today on the Garmin account's calendar, which is what Garmin keys days by.

        Never the container's date: the Dockerfile bootstraps ``TZ=UTC``, and a UTC
        date is a day ahead for a western evening and a day behind for an eastern
        morning -- either asking for a calendarDate not yet begun, or missing the
        night that just ended.

        Before the first profile import there may be no zone to resolve. This is a
        download *range*, not a timestamp conversion, so falling back to the UTC
        date corrupts nothing: at worst the edge day lands one sync later, and this
        same sync imports the zone that fixes it.
        """
        now = self._clock()
        try:
            zone = resolve_home_tz(
                configured=self._settings.home_tz, stored=read_stored_time_zone(self._garmin_db)
            )
        except TimeZoneUnresolved as exc:
            logger.warning(
                "Home timezone not known yet (%s); planning downloads against the UTC date "
                "until the profile import supplies it.",
                exc,
            )
            return now.astimezone(dt.UTC).date()
        return now.astimezone(zone).date()

    def download_plan(self, *, today: dt.date | None = None) -> dict[str, tuple[dt.date, int]]:
        """Per-stat ``(start, days)``, forward from the newest row through today."""
        today = today or self._home_today()
        plan: dict[str, tuple[dt.date, int]] = {}
        for stat in self._config.enabled_stats():
            source = STAT_TABLES.get(stat.name)
            if source is None:
                continue
            kind, table, column = source
            floor, _ = self._config.stat_start_date(stat.name)
            plan[stat.name] = incremental_range(
                latest=table.latest_time(self._db_for(kind), column),
                today=today,
                floor=floor,
            )
        return plan

    def _make_download(self) -> Any:
        download = self._bindings.download(self._config)
        # GarminDB constructs its auth adapter with the default mfa_prompt, a
        # blocking input() on stdin. In the steady state login uses the cached
        # token and never reaches it, but a container must not gamble on that.
        download.garmin.mfa_prompt = _refuse_mfa
        return download

    def _fetch(self, download: Any, stat: str, date: dt.date, days: int) -> None:
        """One statistic's files for ``days`` days starting at ``date``."""
        if stat == "monitoring":
            download.get_daily_summaries(self._config.get_monitoring_dir, date, days, False)
            download.get_hydration(self._config.get_monitoring_dir, date, days, False)
            # A day per call, because Download.get_monitoring makes a mkdtemp() for
            # each day, leaves that day's wellness zip in it, never removes it, and
            # keeps only the *last* one on download.temp_dir. Every sync re-fetches
            # the recent days, so a long-lived container's /tmp would otherwise grow
            # without bound -- faster the more often the sync runs. One day per call
            # makes download.temp_dir exactly the directory to remove.
            for offset in range(days):
                download.get_monitoring(
                    self._config.get_monitoring_dir, date + dt.timedelta(days=offset), 1
                )
                self._discard_temp_dir(download)
        elif stat == "sleep":
            download.get_sleep(self._config.get_sleep_dir(), date, days, False)
        elif stat == "rhr":
            download.get_rhr(self._config.get_rhr_dir(), date, days, False)
        elif stat == "hrv":
            download.get_hrv(self._config.get_rhr_dir(), date, days, False)

    @staticmethod
    def _discard_temp_dir(download: Any) -> None:
        """Remove the scratch directory GarminDB left behind for one wellness zip."""
        temp_dir = getattr(download, "temp_dir", None)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _login(self) -> Any:
        download = self._make_download()
        if not download.login():
            # login() returns False rather than raising; unchecked, the sync would
            # carry on and "succeed" having fetched nothing at all.
            raise RuntimeError(
                "Could not log in to Garmin Connect. The saved token may have expired -- "
                "re-link the account at /setup."
            )
        return download

    def _run_plan(
        self, plan: dict[str, tuple[dt.date, int]], stop: Event, progress: ProgressSink, verb: str
    ) -> None:
        download = self._login()
        total = len(plan)
        for index, (stat, (date, days)) in enumerate(plan.items()):
            if stop.is_set():
                logger.info("Stop requested; ending the download before %s.", stat)
                return
            if days <= 0:
                logger.info("Nothing to download for %s.", stat)
                continue
            # Reported before the call, not after: this is the only signal anyone
            # gets while a stat sleeps a second per day for the next twenty minutes.
            progress(
                f"{verb} {STAT_LABELS.get(stat, stat)} ({days} days from {date})", index, total
            )
            logger.info("Downloading %s from %s for %s days.", stat, date, days)
            self._fetch(download, stat, date, days)
        progress(f"{verb} finished", total, total)

    def download(self, stop: Event, progress: ProgressSink = no_progress) -> None:
        """Fetch JSON/FIT files forward from each stat's newest row.

        Each stat sleeps a second per day and retries five times with backoff, so
        a first backfill runs for tens of minutes. ``stop`` is checked between
        stats, which is the only interruption point GarminDB actually offers.
        """
        self._run_plan(self.download_plan(), stop, progress, "Downloading")

    def backfill_plan(self) -> dict[str, tuple[dt.date, int]]:
        """Per-stat ``(start, days)`` for history *older* than what we hold.

        The complement of :meth:`download_plan`, which only ever moves forward
        from the newest row -- so lowering the configured floor otherwise has no
        effect at all on a metric that already holds data.

        A metric with no rows is deliberately absent: ``incremental_range``
        already starts an empty table at the floor, so backfilling it here would
        just duplicate a normal sync at twice the cost.
        """
        plan: dict[str, tuple[dt.date, int]] = {}
        for stat, coverage in self.stat_coverage().items():
            if not coverage.enabled or not coverage.has_gap:
                continue
            floor = dt.date.fromisoformat(coverage.floor)
            plan[stat] = (floor, coverage.missing_days)
        return plan

    def backfill(self, stop: Event, progress: ProgressSink = no_progress) -> None:
        """Fetch only the older range each enabled metric is missing."""
        plan = self.backfill_plan()
        if not plan:
            logger.info("Nothing to backfill: every enabled metric reaches its start date.")
            progress("Nothing to backfill", 0, 0)
            return
        self._run_plan(plan, stop, progress, "Backfilling")

    # -- import ---------------------------------------------------------------

    @staticmethod
    def _process(step: Any, processor: Any = None) -> None:
        if step.file_count() <= 0:
            return
        if processor is None:
            step.process()
        else:
            step.process_files(processor)

    def _measurement_system(self) -> Any:
        try:
            return Attributes.measurements_type(self._garmin_db)
        except Exception as exc:
            # Only reachable before the first profile import has ever succeeded.
            logger.warning("Could not read measurement_system (%s); importing without it.", exc)
            return None

    def import_(
        self,
        stop: Event,
        progress: ProgressSink = no_progress,
        *,
        latest: bool = IMPORT_LATEST,
    ) -> None:
        """Import the downloaded files, profile first.

        ``latest=False`` reimports the whole retained corpus rather than the last
        24 hours of it, which is what a rebuild needs.
        """
        fit_dir = self._config.get_fit_files_dir()
        monitoring_dir = self._config.get_monitoring_base_dir()
        dbp = self._db_params

        # The profile importers come first so measurement_system exists before
        # anything reads it. GarminPersonalInformation is also what writes the
        # account's IANA timezone into attributes.time_zone.
        profile_steps = (
            ("user settings", self._bindings.user_settings(dbp, fit_dir, DEBUG)),
            ("personal information", self._bindings.personal_information(dbp, fit_dir, DEBUG)),
            ("social profile", self._bindings.social_profile(dbp, fit_dir, DEBUG)),
        )
        progress("Importing profile", 0, 4)
        for name, step in profile_steps:
            if stop.is_set():
                logger.info("Stop requested; ending the import before %s.", name)
                return
            self._process(step)

        measurement_system = self._measurement_system()

        if stop.is_set():
            return
        progress("Importing heart rate and daily activity", 1, 4)
        self._process(
            self._bindings.summary(dbp, monitoring_dir, latest, measurement_system, DEBUG)
        )
        self._process(
            self._bindings.hydration(dbp, monitoring_dir, latest, measurement_system, DEBUG)
        )
        self._process(
            self._bindings.monitoring_fit(monitoring_dir, latest, measurement_system, DEBUG),
            self._bindings.monitoring_processor(dbp, self.plugin_manager, DEBUG),
        )

        if stop.is_set():
            return
        progress("Importing sleep", 2, 4)
        # Prefer Garmin Connect's JSON; fall back to FIT sleep files when there is none.
        sleep_json = self._bindings.sleep_json(dbp, self._config.get_sleep_dir(), latest, DEBUG)
        if sleep_json.file_count() > 0:
            sleep_json.process()
        else:
            self._process(
                self._bindings.sleep_fit(
                    monitoring_dir, latest=False, measurement_system=measurement_system, debug=DEBUG
                ),
                self._bindings.sleep_processor(dbp),
            )

        if stop.is_set():
            return
        progress("Importing resting heart rate and HRV", 3, 4)
        rhr_dir = self._config.get_rhr_dir()
        self._process(self._bindings.rhr(dbp, rhr_dir, latest, DEBUG))
        # HRV tends to land in the same place as RHR.
        self._process(self._bindings.hrv(dbp, rhr_dir, latest, DEBUG))

    # -- rebuild --------------------------------------------------------------

    def rebuild(self, stop: Event, progress: ProgressSink = no_progress) -> None:
        """Delete both SQLite files and reimport the whole retained corpus.

        The owner's way out of a schema mismatch. There is no download: persisting
        the raw JSON/FIT tree is exactly what makes this a local reimport instead
        of hours of re-fetching.

        The order matters. The files are deleted *before* any DB object is
        constructed, because constructing one against the stale schema is what
        raises in the first place; afterwards ``create_all`` builds the current
        schema from nothing. Any handle this adapter already cached would point at
        the deleted inode, so it is dropped too.
        """
        logger.warning("Rebuilding the GarminDB databases in %s.", self._db_params.db_path)
        progress("Deleting the local databases", 0, 0)
        self._handles = None
        GarminDb.delete_db(self._db_params)
        MonitoringDb.delete_db(self._db_params)
        self.import_(stop, progress, latest=False)
        if stop.is_set():
            logger.warning("Stop requested during the rebuild; skipping the analyze phase.")
            return
        progress("Building summaries", 0, 0)
        self.analyze()

    # -- analyze --------------------------------------------------------------

    def _can_analyze(self) -> bool:
        """Whether ``Analyze()`` can be constructed at all.

        ``Analyze.__init__`` ends with ``unit_strings[measurements_type(garmin_db)]``,
        and ``measurements_type`` returns an **unhashable** ``UnknownEnumValue``
        when the ``attributes`` row is missing -- so the lookup raises a bare
        ``TypeError``, not a ``KeyError``. The row only exists once a profile
        import has succeeded, which is exactly what a freshly rebuilt database has
        not necessarily had yet.
        """
        system = self._measurement_system()
        try:
            return system in fitfile.units.unit_strings
        except TypeError:
            return False

    def analyze(self) -> None:
        """Build the summary tables and dynamic views.

        Skipped rather than fatal when the measurement system is unknown: analyze
        only builds summary tables and views, none of which the serving layer
        reads, so losing it degrades nothing we serve -- whereas letting it raise
        would turn an otherwise complete sync or rebuild into a reported failure.
        """
        if not self._can_analyze():
            logger.warning(
                "Skipping the analyze phase: attributes.measurement_system is not set yet, and "
                "Analyze() cannot be constructed without it. It will run on the next sync once a "
                "profile import has succeeded."
            )
            return
        analyze = self._bindings.analyze(self._config, DEBUG)
        analyze.summary()
        analyze.create_dynamic_views()
