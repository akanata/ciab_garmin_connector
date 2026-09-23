"""Day-keyed series: sleep score and resting heart rate."""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.daily import build_resting_heart_rate
from garmin_health.providers.garmindb.daily import build_sleep_score
from garmin_health.providers.garmindb.daily import has_resting_heart_rate
from garmin_health.providers.garmindb.daily import has_sleep_score
from garmin_health.providers.garmindb.settings import GarminDbSettings
from tests.providers.garmindb.fixtures import build_fixture


class TestSleepScore:
    def test_one_sample_per_night(self, corpus_settings: GarminDbSettings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=3, sleep_score=82)
        with GarminConnection(corpus_settings) as conn:
            samples = build_sleep_score(conn, None, None, None)
        assert [s.value for s in samples] == [82.0, 82.0, 82.0]

    def test_nights_without_a_score_are_skipped_rather_than_zeroed(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """sleep.score is nullable, and a night Garmin did not score is not a
        night that scored zero."""
        build_fixture(corpus_settings.health_data_dir, nights=3, sleep_score=None)
        with GarminConnection(corpus_settings) as conn:
            assert build_sleep_score(conn, None, None, None) == []

    def test_a_day_keyed_sample_lands_on_local_midnight_expressed_in_utc(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Local midnight in Denver in June is 06:00Z, not 00:00Z. Emitting 00:00Z
        would be a second, inconsistent convention that places the sample on the
        wrong local day. A consumer bucketing by UTC date is off by one in western
        zones; that is inherent, because the spec has no date-valued sample type."""
        fixture = build_fixture(
            corpus_settings.health_data_dir,
            nights=1,
            last_wake_day=dt.date(2026, 6, 15),
            sleep_score=82,
        )
        with GarminConnection(corpus_settings) as conn:
            samples = build_sleep_score(conn, None, None, None)
        assert samples[0].timestamp == dt.datetime(2026, 6, 15, 6, 0, tzinfo=dt.UTC)
        assert samples[0].timestamp.date() == fixture.newest.wake_day

    def test_the_probe_follows_the_data(self, corpus_settings: GarminDbSettings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, sleep_score=None)
        with GarminConnection(corpus_settings) as conn:
            assert has_sleep_score(conn) is False


class TestRestingHeartRate:
    def test_it_reads_the_resting_hr_table(self, corpus_settings: GarminDbSettings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=3, resting_hr=True)
        with GarminConnection(corpus_settings) as conn:
            samples = build_resting_heart_rate(conn, None, None, None)
        assert [s.value for s in samples] == [52.0, 53.0, 54.0]

    def test_it_falls_back_to_the_daily_summary_column(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """resting_hr and daily_summary come from different downloads, so a corpus
        can easily have one and not the other."""
        build_fixture(
            corpus_settings.health_data_dir, nights=3, resting_hr=False, daily_summary_rhr=True
        )
        with GarminConnection(corpus_settings) as conn:
            samples = build_resting_heart_rate(conn, None, None, None)
        assert [s.value for s in samples] == [60.0, 61.0, 62.0]

    def test_the_fallback_is_logged_when_it_fires(
        self, corpus_settings: GarminDbSettings, caplog: pytest.LogCaptureFixture
    ) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, daily_summary_rhr=True)
        with GarminConnection(corpus_settings) as conn:
            with caplog.at_level(logging.INFO, logger="garmin_health.providers.garmindb.daily"):
                build_resting_heart_rate(conn, None, None, None)
        assert "daily_summary" in caplog.text

    def test_the_primary_table_wins_when_both_are_present(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        build_fixture(
            corpus_settings.health_data_dir, nights=3, resting_hr=True, daily_summary_rhr=True
        )
        with GarminConnection(corpus_settings) as conn:
            samples = build_resting_heart_rate(conn, None, None, None)
        assert [s.value for s in samples] == [52.0, 53.0, 54.0]

    def test_neither_table_means_an_empty_series(self, corpus_settings: GarminDbSettings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=3)
        with GarminConnection(corpus_settings) as conn:
            assert build_resting_heart_rate(conn, None, None, None) == []

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [({"resting_hr": True}, 54.0), ({"daily_summary_rhr": True}, 62.0)],
    )
    def test_the_window_is_honoured_on_both_paths(
        self, corpus_settings: GarminDbSettings, kwargs: dict[str, bool], expected: float
    ) -> None:
        """Day-keyed rows are filtered on local midnight expressed in UTC, so the
        fallback must not quietly widen the window the primary path applied."""
        build_fixture(corpus_settings.health_data_dir, nights=3, **kwargs)
        newest_midnight = dt.datetime(2026, 6, 15, 6, 0, tzinfo=dt.UTC)
        with GarminConnection(corpus_settings) as conn:
            samples = build_resting_heart_rate(conn, newest_midnight, None, None)
        assert [s.value for s in samples] == [expected]

    def test_the_probe_sees_either_table(self, corpus_settings: GarminDbSettings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, daily_summary_rhr=True)
        with GarminConnection(corpus_settings) as conn:
            assert has_resting_heart_rate(conn) is True

    def test_the_probe_is_false_with_neither(self, corpus_settings: GarminDbSettings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1)
        with GarminConnection(corpus_settings) as conn:
            assert has_resting_heart_rate(conn) is False
