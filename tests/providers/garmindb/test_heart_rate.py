"""The heart-rate and HRV builders, and the sub-series a sleep session carries."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from health_data_service import HRV_RMSSD
from health_data_service import HeartRate

from garmin_health.config import Settings
from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.heart_rate import build_heart_rate
from garmin_health.providers.garmindb.heart_rate import build_hrv_rmssd
from garmin_health.providers.garmindb.heart_rate import heart_rate_stats
from garmin_health.providers.garmindb.heart_rate import session_heart_rate
from garmin_health.providers.garmindb.heart_rate import session_hrv
from garmin_health.providers.garmindb.heart_rate import window_hrv_average
from tests.providers.garmindb.fixtures import HEART_RATE_ROWS
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import HRV_ROWS
from tests.providers.garmindb.fixtures import Fixture
from tests.providers.garmindb.fixtures import build_fixture
from tests.providers.garmindb.fixtures import heart_rate_at
from tests.providers.garmindb.fixtures import hrv_at
from tests.providers.garmindb.fixtures import mean


@pytest.fixture
def corpus_settings(tmp_path: Path) -> Settings:
    return Settings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


@pytest.fixture
def night(corpus_settings: Settings) -> Fixture:
    return build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True, hrv=True)


class TestHeartRateSeries:
    def test_a_full_night_is_returned_undecimated(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        """720 rows a day against MAX_LIMIT of 50k: in practice a day is never
        decimated, so this only bites on multi-week requests."""
        with GarminConnection(corpus_settings) as conn:
            samples = build_heart_rate(conn, night.newest.start_utc, night.newest.end_utc, None)
        assert len(samples) == HEART_RATE_ROWS

    def test_a_limit_of_ten_keeps_ten_real_readings(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        with GarminConnection(corpus_settings) as conn:
            samples = build_heart_rate(conn, night.newest.start_utc, night.newest.end_utc, 10)

        assert len(samples) == 10
        assert samples[0].timestamp == night.newest.start_utc
        assert [s.timestamp for s in samples] == sorted(s.timestamp for s in samples)
        assert all(type(s.value) is float for s in samples)
        assert {s.value for s in samples} <= {float(heart_rate_at(i)) for i in range(20)}

    def test_the_integer_column_is_cast_to_float(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        """monitoring_hr.heart_rate is an Integer column, but the spec's HeartRate
        carries list[Sample[float]]."""
        with GarminConnection(corpus_settings) as conn:
            samples = build_heart_rate(conn, None, None, 1)
        assert type(samples[0].value) is float


class TestHrvSeries:
    def test_it_reads_the_rmssd_column(self, corpus_settings: Settings, night: Fixture) -> None:
        with GarminConnection(corpus_settings) as conn:
            samples = build_hrv_rmssd(conn, night.newest.start_utc, night.newest.end_utc, None)
        assert len(samples) == HRV_ROWS
        assert samples[0].value == hrv_at(0)


class TestSessionSubSeries:
    def test_heart_rate_is_sliced_to_the_session_window(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        start = night.newest.start_utc
        with GarminConnection(corpus_settings) as conn:
            series = conn.read(
                lambda g, m: session_heart_rate(m, conn.tz, start, start + dt.timedelta(hours=1))
            )
        assert isinstance(series, HeartRate)
        assert series.source == "garmin"
        assert series.unit == "bpm"
        assert len(series.samples) == 30

    def test_hrv_is_sliced_to_the_session_window_and_carries_its_unit(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        """The spec defaults HRV_RMSSD.unit to None; the column is RMSSD in ms."""
        start = night.newest.start_utc
        with GarminConnection(corpus_settings) as conn:
            series = conn.read(
                lambda g, m: session_hrv(m, conn.tz, start, start + dt.timedelta(hours=1))
            )
        assert isinstance(series, HRV_RMSSD)
        assert series.unit == "ms"
        assert len(series.samples) == 12

    def test_a_window_with_no_rows_yields_none_rather_than_an_empty_series(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        """None and an empty series are different claims: 'no sensor data' versus
        'a sensor that recorded nothing'."""
        far = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
        with GarminConnection(corpus_settings) as conn:
            assert (
                conn.read(
                    lambda g, m: session_heart_rate(m, conn.tz, far, far + dt.timedelta(hours=8))
                )
                is None
            )
            assert (
                conn.read(lambda g, m: session_hrv(m, conn.tz, far, far + dt.timedelta(hours=8)))
                is None
            )

    def test_sub_series_are_capped(
        self, corpus_settings: Settings, night: Fixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An 8-hour night is ~240 rows, so the cap only exists so one corrupt
        window cannot make a session unbounded."""
        monkeypatch.setattr("garmin_health.providers.garmindb.heart_rate.MAX_SESSION_SUBSERIES", 20)
        with GarminConnection(corpus_settings) as conn:
            series = conn.read(
                lambda g, m: session_heart_rate(
                    m, conn.tz, night.newest.start_utc, night.newest.end_utc
                )
            )
        assert series is not None
        assert len(series.samples) == 20


class TestSessionScalars:
    def test_stats_come_from_garmindbs_own_helper(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        """MonitoringHeartRate.get_stats already passes ignore_le_zero=True, which
        is what keeps a 0 bpm dropout out of the minimum."""
        with GarminConnection(corpus_settings) as conn:
            average, lowest = conn.read(
                lambda g, m: heart_rate_stats(
                    m, conn.tz, night.newest.start_utc, night.newest.end_utc
                )
            )
        expected = [float(heart_rate_at(i)) for i in range(HEART_RATE_ROWS)]
        assert average == pytest.approx(mean(expected))
        assert lowest == pytest.approx(min(expected))

    def test_absent_stats_are_none_not_zero(self, corpus_settings: Settings) -> None:
        """Test 'is not None', never truthiness: a real average can be 0 only if
        the watch was off, and that is still not the same as no data."""
        build_fixture(corpus_settings.health_data_dir, nights=1)
        with GarminConnection(corpus_settings) as conn:
            average, lowest = conn.read(
                lambda g, m: heart_rate_stats(
                    m,
                    conn.tz,
                    dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                    dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
                )
            )
        assert average is None
        assert lowest is None

    def test_the_hrv_average_is_over_exactly_the_session_window(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        """Not Hrv.last_night_avg: computing it over the identical window as the
        hrv sub-series is what lets a consumer average session.hrv.samples and get
        the same number back. Hrv.last_night_avg is also an Integer column, and is
        keyed on Garmin's own day, which attaches the wrong night's HRV to any
        session whose calendar attribution is off by one."""
        with GarminConnection(corpus_settings) as conn:
            average = conn.read(
                lambda g, m: window_hrv_average(
                    m, conn.tz, night.newest.start_utc, night.newest.end_utc
                )
            )
        assert average == pytest.approx(mean([hrv_at(i) for i in range(HRV_ROWS)]))

    def test_the_hrv_average_matches_averaging_the_sub_series(
        self, corpus_settings: Settings, night: Fixture
    ) -> None:
        start, end = night.newest.start_utc, night.newest.end_utc
        with GarminConnection(corpus_settings) as conn:
            average, series = conn.read(
                lambda g, m: (
                    window_hrv_average(m, conn.tz, start, end),
                    session_hrv(m, conn.tz, start, end),
                )
            )
        assert series is not None
        assert average == pytest.approx(mean([s.value for s in series.samples]))

    def test_no_hrv_rows_means_no_average(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        with GarminConnection(corpus_settings) as conn:
            assert (
                conn.read(
                    lambda g, m: window_hrv_average(
                        m,
                        conn.tz,
                        dt.datetime(2026, 6, 14, 23, tzinfo=dt.UTC),
                        dt.datetime(2026, 6, 15, 7, tzinfo=dt.UTC),
                    )
                )
                is None
            )
