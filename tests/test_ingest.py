"""Tests for the GarminDB adapter, against a real SQLite corpus and fake collaborators.

Nothing here touches the network. The GarminDB classes are injected as Bindings so
the sequence, the incremental ranges and the pitfalls can all be asserted.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from threading import Event

import pytest

from garmin_health.config import Settings
from garmin_health.garmin.ingest import Bindings
from garmin_health.garmin.ingest import GarminDbIngest
from garmin_health.garmin_config import ensure_config
from tests.fixtures import build_fixture


class FakeStep:
    """Stands in for a GarminDB importer: file_count() then process()/process_files()."""

    def __init__(self, name: str, log: list[str], *, files: int = 1) -> None:
        self.name = name
        self.log = log
        self.files = files
        self.processors: list[object] = []

    def file_count(self) -> int:
        return self.files

    def process(self) -> None:
        self.log.append(self.name)

    def process_files(self, processor: object) -> None:
        self.processors.append(processor)
        self.log.append(self.name)


class FakeDownload:
    def __init__(self, log: list[str], *, login_ok: bool = True) -> None:
        self.log = log
        self.login_ok = login_ok
        self.calls: list[tuple[str, dt.date, int]] = []
        self.garmin = type("Adapter", (), {"mfa_prompt": lambda: "000000"})()

    def login(self) -> bool:
        self.log.append("login")
        return self.login_ok

    def _record(self, name: str, date: dt.date, days: int) -> None:
        self.log.append(name)
        self.calls.append((name, date, days))

    def get_daily_summaries(self, directory_func, date, days, overwrite):  # noqa: ANN001, ANN201
        self._record("daily_summaries", date, days)

    def get_hydration(self, directory_func, date, days, overwrite):  # noqa: ANN001, ANN201
        self._record("hydration", date, days)

    def get_monitoring(self, directory_func, date, days):  # noqa: ANN001, ANN201
        self._record("monitoring", date, days)

    def get_sleep(self, directory, date, days, overwrite):  # noqa: ANN001, ANN201
        self._record("sleep", date, days)

    def get_rhr(self, directory, date, days, overwrite):  # noqa: ANN001, ANN201
        self._record("rhr", date, days)

    def get_hrv(self, directory, date, days, overwrite):  # noqa: ANN001, ANN201
        self._record("hrv", date, days)


class FakeAnalyze:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def summary(self) -> None:
        self.log.append("summary")

    def create_dynamic_views(self) -> None:
        self.log.append("create_dynamic_views")


def make_ingest(
    tmp_path: Path,
    *,
    log: list[str],
    files: int = 1,
    login_ok: bool = True,
    sleep_json_files: int | None = None,
) -> GarminDbIngest:
    settings = Settings(app_data_dir=tmp_path / "appdata")
    ensure_config(settings, user="rider@example.com")

    def step(name: str, count: int = files):
        return lambda *a, **k: FakeStep(name, log, files=count)

    bindings = Bindings(
        download=lambda gc_config: FakeDownload(log, login_ok=login_ok),
        user_settings=step("user_settings"),
        personal_information=step("personal_information"),
        social_profile=step("social_profile"),
        summary=step("summary_data"),
        hydration=step("hydration_data"),
        monitoring_fit=step("monitoring_fit"),
        monitoring_processor=lambda *a, **k: "monitoring-processor",
        sleep_json=step("sleep_json", files if sleep_json_files is None else sleep_json_files),
        sleep_fit=step("sleep_fit"),
        sleep_processor=lambda *a, **k: "sleep-processor",
        rhr=step("rhr_data"),
        hrv=step("hrv_data"),
        analyze=lambda gc_config, debug: FakeAnalyze(log),
    )
    return GarminDbIngest(settings, bindings=bindings)


class TestTableStats:
    def test_counts_rows_in_a_real_corpus(self, tmp_path: Path) -> None:
        settings = Settings(app_data_dir=tmp_path / "appdata")
        ensure_config(settings, user="rider@example.com")
        build_fixture(settings.health_data_dir, nights=3, heart_rate=True)

        stats = GarminDbIngest(settings).table_stats()
        assert stats["sleep"].rows == 3
        assert stats["sleep_events"].rows == 24
        assert stats["monitoring_hr"].rows == 240

    def test_reports_the_latest_stored_value_as_an_iso_string(self, tmp_path: Path) -> None:
        """It is a naive local wall clock, not an instant, so it is deliberately not
        presented as a timestamp the caller might convert."""
        settings = Settings(app_data_dir=tmp_path / "appdata")
        ensure_config(settings, user="rider@example.com")
        fixture = build_fixture(settings.health_data_dir, nights=2)

        stats = GarminDbIngest(settings).table_stats()
        assert stats["sleep"].latest == fixture.newest.day_column.isoformat()

    def test_an_empty_corpus_reports_zeroes_rather_than_failing(self, tmp_path: Path) -> None:
        """A fresh install before the first sync is a normal, serviceable state:
        constructing the DB objects creates the schema."""
        settings = Settings(app_data_dir=tmp_path / "appdata")
        ensure_config(settings, user="rider@example.com")

        stats = GarminDbIngest(settings).table_stats()
        assert stats["sleep"] == stats["sleep"].__class__(rows=0, latest=None)
        assert all(stat.rows == 0 for stat in stats.values())


class TestDownloadPlan:
    def test_falls_back_to_the_configured_start_date_on_an_empty_corpus(
        self, tmp_path: Path
    ) -> None:
        settings = Settings(app_data_dir=tmp_path / "appdata")
        ensure_config(settings, user="rider@example.com")
        plan = GarminDbIngest(settings).download_plan(today=dt.date(2026, 6, 15))
        assert plan["sleep"][0] == dt.date(2019, 12, 31)

    def test_uses_the_incremental_rule_once_rows_exist(self, tmp_path: Path) -> None:
        settings = Settings(app_data_dir=tmp_path / "appdata")
        ensure_config(settings, user="rider@example.com")
        build_fixture(settings.health_data_dir, nights=2, last_wake_day=dt.date(2026, 6, 15))

        plan = GarminDbIngest(settings).download_plan(today=dt.date(2026, 6, 16))
        # Newest sleep.day is 2026-06-15, so start one day before it.
        assert plan["sleep"] == (dt.date(2026, 6, 14), 2)

    def test_covers_every_enabled_stat(self, tmp_path: Path) -> None:
        settings = Settings(app_data_dir=tmp_path / "appdata")
        ensure_config(settings, user="rider@example.com")
        plan = GarminDbIngest(settings).download_plan(today=dt.date(2026, 6, 15))
        assert set(plan) == {"monitoring", "sleep", "rhr", "hrv"}


class TestDownload:
    def test_logs_in_before_fetching_anything(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log).download(Event())
        assert log[0] == "login"

    def test_fetches_every_enabled_stat(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log).download(Event())
        assert log == ["login", "daily_summaries", "hydration", "monitoring", "sleep", "rhr", "hrv"]

    def test_a_failed_login_stops_the_sync_loudly(self, tmp_path: Path) -> None:
        """Download.login() returns False rather than raising; left unchecked the
        sync would carry on and 'succeed' having fetched nothing."""
        log: list[str] = []
        with pytest.raises(RuntimeError, match="log in"):
            make_ingest(tmp_path, log=log, login_ok=False).download(Event())
        assert log == ["login"]

    def test_a_stop_request_ends_the_download_between_stats(self, tmp_path: Path) -> None:
        """A download sleeps a second per day with retries, so between stats is the
        only interruption point that actually exists."""
        log: list[str] = []
        stop = Event()
        stop.set()
        make_ingest(tmp_path, log=log).download(stop)
        assert log == ["login"]

    def test_refuses_an_interactive_mfa_prompt(self, tmp_path: Path) -> None:
        """GarminDB's auth adapter defaults to a blocking input() on stdin, which in
        a container would hang a worker thread for ever."""
        log: list[str] = []
        ingest = make_ingest(tmp_path, log=log)
        download = ingest._make_download()
        with pytest.raises(RuntimeError, match="MFA"):
            download.garmin.mfa_prompt()


class TestImport:
    def test_imports_the_profile_before_anything_that_reads_it(self, tmp_path: Path) -> None:
        """measurement_system is read from attributes right after the profile
        importers, so they must run first."""
        log: list[str] = []
        make_ingest(tmp_path, log=log).import_(Event())
        assert log[:3] == ["user_settings", "personal_information", "social_profile"]

    def test_runs_the_documented_sequence(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log).import_(Event())
        assert log == [
            "user_settings",
            "personal_information",
            "social_profile",
            "summary_data",
            "hydration_data",
            "monitoring_fit",
            "sleep_json",
            "rhr_data",
            "hrv_data",
        ]

    def test_falls_back_to_fit_sleep_when_there_is_no_json(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log, sleep_json_files=0).import_(Event())
        assert "sleep_fit" in log
        assert "sleep_json" not in log

    def test_skips_importers_with_no_files(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log, files=0).import_(Event())
        assert log == []

    def test_passes_a_real_plugin_manager_to_the_monitoring_processor(self, tmp_path: Path) -> None:
        """MonitoringFitFileProcessor.write_file dereferences plugin_manager
        unconditionally; None raises AttributeError partway through an import."""
        log: list[str] = []
        ingest = make_ingest(tmp_path, log=log)
        assert ingest.plugin_manager is not None

    def test_a_stop_request_ends_the_import_between_steps(self, tmp_path: Path) -> None:
        log: list[str] = []
        stop = Event()
        stop.set()
        make_ingest(tmp_path, log=log).import_(stop)
        assert log == []


class TestAnalyze:
    def test_summarises_then_creates_views(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log).analyze()
        assert log == ["summary", "create_dynamic_views"]


class TestConfigSafety:
    def test_constructing_the_ingest_never_reaches_the_config_managers_sys_exit(
        self, tmp_path: Path
    ) -> None:
        """GarminConnectConfigManager exits the process on a malformed config, which
        in a server is unrecoverable. The ingest must repair it first."""
        settings = Settings(app_data_dir=tmp_path / "appdata")
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        settings.garmin_config_file.write_text("{ not json")

        ingest = GarminDbIngest(settings)
        assert ingest.table_stats()["sleep"].rows == 0


class TestUncoveredBranches:
    def test_skips_a_stat_that_is_already_current(self, tmp_path: Path) -> None:
        """incremental_range clamps a future-dated row to zero days; asking Garmin
        for that range would be a pointless round trip."""
        log: list[str] = []
        ingest = make_ingest(tmp_path, log=log)
        ingest.download_plan = lambda **_: {"sleep": (dt.date(2026, 6, 15), 0)}  # type: ignore[method-assign]
        ingest.download(Event())
        assert log == ["login"]

    def test_imports_without_a_measurement_system_rather_than_failing(self, tmp_path: Path) -> None:
        """Before the very first profile import there is no measurement_system row,
        and reading it must not abort the import that would create one."""
        log: list[str] = []
        ingest = make_ingest(tmp_path, log=log)
        ingest._garmin_db = object()  # type: ignore[assignment]
        ingest.import_(Event())
        assert "rhr_data" in log

    def test_a_stop_request_ends_the_import_before_sleep(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log).import_(StopAfterStep(log, "monitoring_fit"))  # type: ignore[arg-type]
        assert "monitoring_fit" in log
        assert "sleep_json" not in log

    def test_a_stop_request_ends_the_import_before_rhr(self, tmp_path: Path) -> None:
        log: list[str] = []
        make_ingest(tmp_path, log=log).import_(StopAfterStep(log, "sleep_json"))  # type: ignore[arg-type]
        assert "sleep_json" in log
        assert "rhr_data" not in log


class StopAfterStep:
    """Reads as 'set' once a named step has run, so the assertion does not depend
    on how many times import_ happens to poll the flag."""

    def __init__(self, log: list[str], after: str) -> None:
        self._log = log
        self._after = after

    def is_set(self) -> bool:
        return self._after in self._log
