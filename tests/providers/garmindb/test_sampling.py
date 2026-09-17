"""The GarminDB window queries: bounds, the scan guard, and the column builder.

The limit policy and decimation these rely on are provider-agnostic and tested
in ``tests/test_limits.py``; what is here is everything that only means anything
against a real SQLite corpus.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import MonitoringHrvValue
from garmindb.garmindb import Sleep
from health_data_service import Sample

from garmin_health import limits
from garmin_health.config import Settings
from garmin_health.limits import WindowTooLarge
from garmin_health.providers.garmindb import sampling
from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.sampling import column_series
from garmin_health.providers.garmindb.sampling import has_rows
from tests.providers.garmindb.fixtures import HEART_RATE_INTERVAL
from tests.providers.garmindb.fixtures import HEART_RATE_ROWS
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import build_fixture
from tests.providers.garmindb.fixtures import heart_rate_at


@pytest.fixture
def corpus_settings(tmp_path: Path) -> Settings:
    return Settings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


class TestColumnSeries:
    def test_it_emits_aware_utc_samples_of_the_cast_type(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            samples = build(conn, None, None, None)

        assert len(samples) == HEART_RATE_ROWS
        assert all(isinstance(s, Sample) for s in samples)
        assert all(s.timestamp.tzinfo == dt.UTC for s in samples)
        assert all(type(s.value) is float for s in samples)
        assert samples[0].value == float(heart_rate_at(0))

    def test_the_window_is_half_open_on_the_real_instants(self, corpus_settings: Settings) -> None:
        """Asked in UTC, answered against naive local rows. If an aware bound ever
        reached SQLite the count here would silently be a wrong subset."""
        fixture = build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        start = fixture.newest.start_utc
        end = start + HEART_RATE_INTERVAL * 10
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")

        with GarminConnection(corpus_settings) as conn:
            samples = build(conn, start, end, None)

        assert len(samples) == 10
        assert samples[0].timestamp == start
        assert samples[-1].timestamp == end - HEART_RATE_INTERVAL

    def test_every_query_bound_reaching_the_database_is_naive(
        self, corpus_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQLAlchemy's SQLite DATETIME bind processor DISCARDS tzinfo, so an aware
        bound mis-filters with no error at all -- it returns a plausible subset."""
        fixture = build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        captured: list[dt.datetime | None] = []
        real = sampling.period_rows

        def spy(session: Any, table: Any, *columns: Any, **kwargs: Any) -> Any:
            captured.extend([kwargs.get("start"), kwargs.get("end")])
            return real(session, table, *columns, **kwargs)

        monkeypatch.setattr(sampling, "period_rows", spy)
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            build(conn, fixture.newest.start_utc, fixture.newest.end_utc, None)

        assert captured
        assert all(bound is None or bound.tzinfo is None for bound in captured)

    def test_it_decimates_to_the_requested_limit(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            samples = build(conn, None, None, 10)

        assert len(samples) == 10
        assert [s.timestamp for s in samples] == sorted(s.timestamp for s in samples)
        assert samples[0].value == float(heart_rate_at(0))
        assert samples[-1].value == float(heart_rate_at(HEART_RATE_ROWS - 1))

    def test_an_empty_window_is_an_empty_list_not_an_error(self, corpus_settings: Settings) -> None:
        """A known metric with no data in range is 200 with samples: [], which the
        consumer reads as 'nothing here'; a 500 would read as 'provider broken'."""
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        far_future = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
        with GarminConnection(corpus_settings) as conn:
            assert build(conn, far_future, far_future + dt.timedelta(days=1), None) == []

    def test_it_reads_the_garmin_database_when_told_to(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=3, sleep_score=82)
        build = column_series(Sleep, Sleep.score, db="garmin", skip_none=True)
        with GarminConnection(corpus_settings) as conn:
            samples = build(conn, None, None, None)
        assert [s.value for s in samples] == [82.0, 82.0, 82.0]

    def test_null_valued_rows_are_skipped_before_decimation(
        self, corpus_settings: Settings
    ) -> None:
        """Filtering in SQL rather than after the fact: dropping Nones later would
        make a limit of N return fewer than N samples for no visible reason."""
        build_fixture(corpus_settings.health_data_dir, nights=3, sleep_score=None)
        build = column_series(Sleep, Sleep.score, db="garmin", skip_none=True)
        with GarminConnection(corpus_settings) as conn:
            assert build(conn, None, None, None) == []

    def test_a_window_too_large_to_scan_is_refused(
        self, corpus_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """413, not a swap storm: the guard bounds the FETCH, not the response.

        The cap is patched on ``garmin_health.limits`` because ``check_scan_cap``
        reads it there at call time -- this builder no longer owns it.
        """
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        monkeypatch.setattr(limits, "MAX_ROWS_SCANNED", 10)
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            with pytest.raises(WindowTooLarge) as caught:
                build(conn, None, None, 5)
        assert str(HEART_RATE_ROWS) in str(caught.value)

    def test_the_guard_counts_rows_rather_than_fetching_them(
        self, corpus_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        monkeypatch.setattr(limits, "MAX_ROWS_SCANNED", 10)

        def must_not_run(*_: Any, **__: Any) -> None:
            raise AssertionError("rows were fetched despite the window being refused")

        monkeypatch.setattr(sampling, "period_rows", must_not_run)
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            with pytest.raises(WindowTooLarge):
                build(conn, None, None, None)


class TestHasRows:
    def test_it_reports_a_populated_table(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        probe = has_rows(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            assert probe(conn) is True

    def test_it_reports_an_empty_table(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        probe = has_rows(MonitoringHrvValue, MonitoringHrvValue.hrv, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            assert probe(conn) is False

    def test_a_table_of_only_nulls_counts_as_empty(self, corpus_settings: Settings) -> None:
        """Advertising an empty metric costs the consumer a wasted round trip,
        because list_metrics_merged is what it uses to decide what to request."""
        build_fixture(corpus_settings.health_data_dir, nights=3, sleep_score=None)
        probe = has_rows(Sleep, Sleep.score, db="garmin")
        with GarminConnection(corpus_settings) as conn:
            assert probe(conn) is False
