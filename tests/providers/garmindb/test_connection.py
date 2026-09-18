"""The GarminDB gateway: lifecycle, faults, and the paired sessions."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import attrs
import pytest
from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import Sleep
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.connection import GarminSchemaMismatch
from garmin_health.providers.garmindb.connection import GarminUnavailable
from garmin_health.providers.garmindb.settings import GarminDbSettings
from garmin_health.providers.garmindb.timezones import TimeZoneUnresolved
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import Fixture
from tests.providers.garmindb.fixtures import build_fixture


def fixture_for(settings: GarminDbSettings, **kwargs: Any) -> Fixture:
    """Build a corpus in the exact place ``settings`` will look for one."""
    return build_fixture(settings.health_data_dir, **kwargs)


class TestFirstBoot:
    def test_it_opens_against_a_directory_that_does_not_exist_yet(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """A container that has never linked an account must still serve. Creating
        the DB objects is what populates the schema, so this is a normal state."""
        assert not corpus_settings.db_dir.exists()
        with GarminConnection(corpus_settings) as conn:
            assert conn.fault is None
            assert (corpus_settings.db_dir / "garmin.db").exists()
            assert (corpus_settings.db_dir / "garmin_monitoring.db").exists()

    def test_an_empty_corpus_reads_as_empty_rather_than_failing(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        with GarminConnection(corpus_settings) as conn:
            assert conn.has_any_data() is False
            rows = conn.read(lambda g, m: Sleep.s_get_for_period(g, None, None))
            assert rows == []

    def test_a_populated_corpus_reports_that_it_has_data(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture_for(corpus_settings, heart_rate=True)
        with GarminConnection(corpus_settings) as conn:
            assert conn.has_any_data() is True


class TestPairedSessions:
    def test_both_databases_are_reachable_from_one_read(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """garmin.db and garmin_monitoring.db are separate files with separate
        engines, so 'one session' means one session per database."""
        fixture_for(corpus_settings, heart_rate=True)
        with GarminConnection(corpus_settings) as conn:
            sleep_rows, hr_rows = conn.read(
                lambda g, m: (
                    len(Sleep.s_get_for_period(g, None, None)),
                    len(MonitoringHeartRate.s_get_for_period(m, None, None)),
                )
            )
        assert sleep_rows == 3
        assert hr_rows == 240

    def test_orm_instances_stay_usable_after_the_session_closes(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """managed_session() is sessionmaker(expire_on_commit=False).begin(), which
        is what lets a builder return rows instead of copying every column out."""
        fixture_for(corpus_settings)
        with GarminConnection(corpus_settings) as conn:
            rows = conn.read(lambda g, m: Sleep.s_get_for_period(g, None, None))
        assert rows[0].day is not None

    def test_table_class_setup_ran_so_time_col_exists(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """DbObject.setup() is only called from DB.init_table() inside DB.__init__.
        Without a constructed MonitoringDb, MonitoringHeartRate has no time_col and
        every period query raises AttributeError."""
        with GarminConnection(corpus_settings):
            assert hasattr(MonitoringHeartRate, "time_col")
            assert MonitoringHeartRate.time_col_name == "timestamp"


class TestTimeZonePolicy:
    def test_the_policy_is_resolved_once_at_open(self, corpus_settings: GarminDbSettings) -> None:
        fixture_for(corpus_settings)
        with GarminConnection(corpus_settings) as conn:
            assert conn.tz.home_tz == ZoneInfo(HOME_TZ_NAME)
            assert conn.tz is conn.tz

    def test_a_configured_home_zone_wins_over_what_garmin_stored(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture_for(corpus_settings, stored_time_zone="Europe/Berlin")
        with GarminConnection(attrs.evolve(corpus_settings, home_tz="Asia/Tokyo")) as conn:
            assert conn.tz.home_tz == ZoneInfo("Asia/Tokyo")

    def test_an_unresolvable_zone_does_not_stop_the_connection_opening(
        self, tmp_path: Path
    ) -> None:
        """Failing to open would take out /setup and /sync/status too, which are
        exactly where the owner would go to fix it."""
        settings = GarminDbSettings(app_data_dir=tmp_path / "appdata")
        with GarminConnection(settings) as conn:
            assert conn.fault is None
            with pytest.raises(TimeZoneUnresolved):
                _ = conn.tz

    def test_an_unresolvable_zone_is_retried_after_a_sync(self, tmp_path: Path) -> None:
        """The account's zone arrives with the first profile import, so the boot
        attempt legitimately runs before the answer exists."""
        settings = GarminDbSettings(app_data_dir=tmp_path / "appdata")
        with GarminConnection(settings) as conn:
            with pytest.raises(TimeZoneUnresolved):
                _ = conn.tz
            fixture_for(settings)
            conn.reset()
            assert conn.tz.home_tz == ZoneInfo(HOME_TZ_NAME)

    def test_the_learned_import_offset_is_carried_on_the_policy(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture_for(corpus_settings, import_tz=dt.UTC)
        with GarminConnection(corpus_settings) as conn:
            assert conn.tz.import_offset == dt.timedelta(hours=6)

    def test_a_configured_import_zone_skips_learning_entirely(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture_for(corpus_settings, import_tz=dt.UTC)
        settings = attrs.evolve(corpus_settings, import_tz="UTC")
        with GarminConnection(settings) as conn:
            assert conn.tz.import_tz == ZoneInfo("UTC")
            assert conn.tz.import_offset == dt.timedelta(0)


class TestSchemaMismatch:
    @staticmethod
    def _break_version(db_file: Path) -> None:
        """What a GarminDB upgrade does to an existing corpus."""
        with sqlite3.connect(db_file) as db:
            db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")

    def test_a_stale_schema_is_a_recorded_fault_not_a_crash(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Refusing to start would make the router restart-loop a container whose
        only problem is a stale schema, and the owner could never reach /setup."""
        fixture_for(corpus_settings)
        self._break_version(corpus_settings.db_dir / "garmin.db")
        with GarminConnection(corpus_settings) as conn:
            assert conn.fault is not None
            assert "rebuild" in conn.fault.lower()

    def test_reads_against_a_faulted_connection_raise_a_typed_error(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture_for(corpus_settings)
        self._break_version(corpus_settings.db_dir / "garmin.db")
        with GarminConnection(corpus_settings) as conn:
            with pytest.raises(GarminSchemaMismatch):
                conn.read(lambda g, m: None)

    def test_a_schema_mismatch_is_a_kind_of_unavailable(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Routes catch the general case; the specific one only shapes the message."""
        assert issubclass(GarminSchemaMismatch, GarminUnavailable)

    def test_a_repaired_corpus_clears_the_fault_on_reset(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture_for(corpus_settings)
        self._break_version(corpus_settings.db_dir / "garmin.db")
        with GarminConnection(corpus_settings) as conn:
            assert conn.fault is not None
            (corpus_settings.db_dir / "garmin.db").unlink()
            conn.reset()
            assert conn.fault is None


class TestReset:
    def test_it_picks_up_a_rebuilt_database_file(self, corpus_settings: GarminDbSettings) -> None:
        """Pooled handles point at the DELETED inode after a rebuild and would go
        on serving the old rows with no error at all."""
        fixture_for(corpus_settings)
        with GarminConnection(corpus_settings) as conn:
            assert conn.read(lambda g, m: len(Sleep.s_get_for_period(g, None, None))) == 3

            (corpus_settings.db_dir / "garmin.db").unlink()
            (corpus_settings.db_dir / "garmin_monitoring.db").unlink()
            fixture_for(corpus_settings, nights=1)

            conn.reset()
            assert conn.read(lambda g, m: len(Sleep.s_get_for_period(g, None, None))) == 1

    def test_it_is_safe_to_call_on_a_connection_that_is_already_closed(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        conn = GarminConnection(corpus_settings)
        conn.close()
        conn.reset()
        assert conn.fault is None
        conn.close()


class TestOperationalErrorRetry:
    def test_one_transient_failure_is_retried_after_a_reset(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture_for(corpus_settings)
        attempts: list[int] = []

        def flaky(g: object, m: object) -> int:
            attempts.append(1)
            if len(attempts) == 1:
                raise OperationalError("SELECT 1", {}, Exception("database is locked"))
            return len(Sleep.s_get_for_period(g, None, None))

        with GarminConnection(corpus_settings) as conn:
            assert conn.read(flaky) == 3
        assert len(attempts) == 2

    def test_it_does_not_retry_forever(self, corpus_settings: GarminDbSettings) -> None:
        """A genuinely broken file would otherwise spin a worker thread until the
        request timed out, with no error ever reaching the consumer."""
        attempts: list[int] = []

        def always_broken(g: object, m: object) -> None:
            attempts.append(1)
            raise OperationalError("SELECT 1", {}, Exception("disk I/O error"))

        with GarminConnection(corpus_settings) as conn:
            with pytest.raises(OperationalError):
                conn.read(always_broken)
        assert len(attempts) == 2

    def test_other_errors_are_not_retried_at_all(self, corpus_settings: GarminDbSettings) -> None:
        attempts: list[int] = []

        def bug(g: object, m: object) -> None:
            attempts.append(1)
            raise ValueError("a bug in a builder")

        with GarminConnection(corpus_settings) as conn:
            with pytest.raises(ValueError, match="a bug in a builder"):
                conn.read(bug)
        assert len(attempts) == 1


def test_readers_wait_out_a_writers_lock_instead_of_failing(
    corpus_settings: GarminDbSettings,
) -> None:
    """The sync engine writes these same files while requests are being served.
    Without busy_timeout, SQLite raises 'database is locked' immediately."""
    with GarminConnection(corpus_settings) as conn:
        timeout = conn.read(lambda g, m: g.execute(text("PRAGMA busy_timeout")).scalar())
    assert timeout >= 5000
