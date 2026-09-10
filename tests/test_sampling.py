"""Decimation, the limit policy, and the generic column-series builder."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import MonitoringHrvValue
from garmindb.garmindb import Sleep
from health_data_service import Sample

from garmin_health.config import DEFAULT_LIMIT
from garmin_health.config import MAX_LIMIT
from garmin_health.config import Settings
from garmin_health.garmin import sampling
from garmin_health.garmin.connection import GarminConnection
from garmin_health.garmin.sampling import InvalidLimit
from garmin_health.garmin.sampling import WindowTooLarge
from garmin_health.garmin.sampling import column_series
from garmin_health.garmin.sampling import decimate
from garmin_health.garmin.sampling import has_rows
from garmin_health.garmin.sampling import resolve_limit
from tests.fixtures import HEART_RATE_INTERVAL
from tests.fixtures import HEART_RATE_ROWS
from tests.fixtures import HOME_TZ_NAME
from tests.fixtures import build_fixture
from tests.fixtures import heart_rate_at


@pytest.fixture
def corpus_settings(tmp_path: Path) -> Settings:
    return Settings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


class TestDecimate:
    def test_a_short_series_is_returned_untouched(self) -> None:
        rows = list(range(5))
        assert decimate(rows, 10) == rows

    def test_no_limit_returns_everything(self) -> None:
        rows = list(range(5))
        assert decimate(rows, None) == rows

    def test_it_keeps_exactly_the_limit(self) -> None:
        assert len(decimate(list(range(HEART_RATE_ROWS)), 10)) == 10

    def test_the_first_and_last_readings_are_always_kept(self) -> None:
        """The consumer asked for a window; both of its ends are the answer."""
        kept = decimate(list(range(HEART_RATE_ROWS)), 10)
        assert kept[0] == 0
        assert kept[-1] == HEART_RATE_ROWS - 1

    def test_the_kept_rows_are_evenly_spaced_and_ascending(self) -> None:
        kept = decimate(list(range(1000)), 11)
        gaps = {b - a for a, b in zip(kept, kept[1:], strict=False)}
        assert gaps == {99, 100}
        assert kept == sorted(kept)

    def test_a_limit_of_one_returns_the_most_recent_reading(self) -> None:
        assert decimate(list(range(HEART_RATE_ROWS)), 1) == [HEART_RATE_ROWS - 1]

    def test_it_never_returns_a_duplicate(self) -> None:
        kept = decimate(list(range(HEART_RATE_ROWS)), 50)
        assert len(set(kept)) == len(kept)

    def test_it_is_selection_not_aggregation(self) -> None:
        """Every emitted value must be a real reading at a real recorded instant.
        A bucket mean would carry a timestamp at which nothing was measured."""
        rows = [object() for _ in range(100)]
        kept = decimate(rows, 7)
        assert all(any(k is r for r in rows) for k in kept)

    def test_an_empty_series_stays_empty(self) -> None:
        assert decimate([], 10) == []


class TestResolveLimit:
    def test_no_limit_becomes_the_default(self) -> None:
        """Applied so an unbounded request cannot build 263k Sample objects."""
        assert resolve_limit(None) == DEFAULT_LIMIT

    def test_a_reasonable_limit_is_honoured(self) -> None:
        assert resolve_limit(200) == 200

    def test_an_excessive_limit_is_clamped_rather_than_refused(self) -> None:
        """limit is a resolution knob, so an over-large one is answerable."""
        assert resolve_limit(MAX_LIMIT * 10) == MAX_LIMIT

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_limit_is_an_error(self, bad: int) -> None:
        """Clamping 0 up to 1 or down to 'everything' both invent an intent."""
        with pytest.raises(InvalidLimit):
            resolve_limit(bad)


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
        """413, not a swap storm: the guard bounds the FETCH, not the response."""
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        monkeypatch.setattr(sampling, "MAX_ROWS_SCANNED", 10)
        build = column_series(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")
        with GarminConnection(corpus_settings) as conn:
            with pytest.raises(WindowTooLarge) as caught:
                build(conn, None, None, 5)
        assert str(HEART_RATE_ROWS) in str(caught.value)

    def test_the_guard_counts_rows_rather_than_fetching_them(
        self, corpus_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        monkeypatch.setattr(sampling, "MAX_ROWS_SCANNED", 10)

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
