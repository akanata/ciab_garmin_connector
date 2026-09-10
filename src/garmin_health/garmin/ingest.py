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
from collections.abc import Callable
from threading import Event
from typing import Any

import attrs
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

from garmin_health.config import Settings
from garmin_health.garmin_config import load_manager

# TableStat is a plain value object on the port between sync.py and this adapter;
# importing it here does not drag garmindb into sync.py, which is what keeps the
# engine testable and GarminDB swappable.
from garmin_health.sync import TableStat
from garmin_health.sync import incremental_range

logger = logging.getLogger(__name__)

# Importers are told latest=True, which means "files whose mtime is in the last
# 24h", NOT "the newest N". That is only safe because the download step has just
# rewritten exactly the files we care about. The cost is that a file which failed
# to import on an earlier run is not retried; row counts in /sync/status are what
# make that visible.
IMPORT_LATEST = True
DEBUG = 0


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

    def __init__(self, settings: Settings, *, bindings: Bindings | None = None) -> None:
        self._settings = settings
        self._bindings = bindings or Bindings()
        # load_manager validates and repairs the JSON first: the bare
        # GarminConnectConfigManager calls sys.exit(-1) on a malformed config,
        # which in a server process nothing can intercept.
        self._config = load_manager(settings)
        self._db_params = self._config.get_db_params()
        # MonitoringFitFileProcessor.write_file dereferences plugin_manager
        # unconditionally, so None raises AttributeError partway through an import.
        self.plugin_manager = PluginManager(self._config.get_plugins_dir(), self._db_params)
        # Constructing a DB runs create_all plus a version check -- it is a write,
        # and it is also what installs cls.time_col on every table class.
        self._garmin_db = GarminDb(self._db_params)
        self._monitoring_db = MonitoringDb(self._db_params)

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

    # -- download -------------------------------------------------------------

    def download_plan(self, *, today: dt.date | None = None) -> dict[str, tuple[dt.date, int]]:
        """Per-stat (start date, days), using GarminDB's own incremental rule."""
        today = today or dt.date.today()
        sources: dict[str, tuple[Any, Any, Any]] = {
            "monitoring": (
                self._monitoring_db,
                MonitoringHeartRate,
                MonitoringHeartRate.heart_rate,
            ),
            "sleep": (self._garmin_db, Sleep, Sleep.total_sleep),
            "rhr": (self._garmin_db, RestingHeartRate, RestingHeartRate.resting_heart_rate),
            "hrv": (self._garmin_db, Hrv, Hrv.day),
        }
        plan: dict[str, tuple[dt.date, int]] = {}
        for stat in self._config.enabled_stats():
            source = sources.get(stat.name)
            if source is None:
                continue
            db, table, column = source
            plan[stat.name] = incremental_range(
                latest=table.latest_time(db, column),
                today=today,
                fallback=self._config.stat_start_date(stat.name),
            )
        return plan

    def _make_download(self) -> Any:
        download = self._bindings.download(self._config)
        # GarminDB constructs its auth adapter with the default mfa_prompt, a
        # blocking input() on stdin. In the steady state login uses the cached
        # token and never reaches it, but a container must not gamble on that.
        download.garmin.mfa_prompt = _refuse_mfa
        return download

    def download(self, stop: Event) -> None:
        """Fetch JSON/FIT files for every enabled stat.

        Each stat sleeps a second per day and retries five times with backoff, so
        a first backfill runs for tens of minutes. ``stop`` is checked between
        stats, which is the only interruption point GarminDB actually offers.
        """
        download = self._make_download()
        if not download.login():
            # login() returns False rather than raising; unchecked, the sync would
            # carry on and "succeed" having fetched nothing at all.
            raise RuntimeError(
                "Could not log in to Garmin Connect. The saved token may have expired -- "
                "re-link the account at /setup."
            )

        plan = self.download_plan()
        for stat, (date, days) in plan.items():
            if stop.is_set():
                logger.info("Stop requested; ending the download before %s.", stat)
                return
            if days <= 0:
                logger.info("Nothing to download for %s.", stat)
                continue
            logger.info("Downloading %s from %s for %s days.", stat, date, days)
            if stat == "monitoring":
                download.get_daily_summaries(self._config.get_monitoring_dir, date, days, False)
                download.get_hydration(self._config.get_monitoring_dir, date, days, False)
                download.get_monitoring(self._config.get_monitoring_dir, date, days)
            elif stat == "sleep":
                download.get_sleep(self._config.get_sleep_dir(), date, days, False)
            elif stat == "rhr":
                download.get_rhr(self._config.get_rhr_dir(), date, days, False)
            elif stat == "hrv":
                download.get_hrv(self._config.get_rhr_dir(), date, days, False)

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

    def import_(self, stop: Event) -> None:
        """Import the downloaded files, profile first."""
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
        for name, step in profile_steps:
            if stop.is_set():
                logger.info("Stop requested; ending the import before %s.", name)
                return
            self._process(step)

        measurement_system = self._measurement_system()

        if stop.is_set():
            return
        self._process(
            self._bindings.summary(dbp, monitoring_dir, IMPORT_LATEST, measurement_system, DEBUG)
        )
        self._process(
            self._bindings.hydration(dbp, monitoring_dir, IMPORT_LATEST, measurement_system, DEBUG)
        )
        self._process(
            self._bindings.monitoring_fit(monitoring_dir, IMPORT_LATEST, measurement_system, DEBUG),
            self._bindings.monitoring_processor(dbp, self.plugin_manager, DEBUG),
        )

        if stop.is_set():
            return
        # Prefer Garmin Connect's JSON; fall back to FIT sleep files when there is none.
        sleep_json = self._bindings.sleep_json(
            dbp, self._config.get_sleep_dir(), IMPORT_LATEST, DEBUG
        )
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
        rhr_dir = self._config.get_rhr_dir()
        self._process(self._bindings.rhr(dbp, rhr_dir, IMPORT_LATEST, DEBUG))
        # HRV tends to land in the same place as RHR.
        self._process(self._bindings.hrv(dbp, rhr_dir, IMPORT_LATEST, DEBUG))

    # -- analyze --------------------------------------------------------------

    def analyze(self) -> None:
        """Build the summary tables and dynamic views."""
        analyze = self._bindings.analyze(self._config, DEBUG)
        analyze.summary()
        analyze.create_dynamic_views()
