"""Sleep sessions: windows, stages, durations, and everything derived from them."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import attrs
import pytest
from garmindb.garmindb import SleepEvents
from health_data_service import IntervalSample
from health_data_service import SleepSession
from health_data_service import SleepStage

from garmin_health.providers.garmindb import sleep as sleep_module
from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.settings import GarminDbSettings
from garmin_health.providers.garmindb.sleep import build_sleep_sessions
from garmin_health.providers.garmindb.vocabulary import reset_unknown_event_log
from tests.providers.garmindb.fixtures import AWAKE
from tests.providers.garmindb.fixtures import DEEP_SLEEP
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import HRV_ROWS
from tests.providers.garmindb.fixtures import JSON_NON_REM_STAGES
from tests.providers.garmindb.fixtures import LIGHT_SLEEP
from tests.providers.garmindb.fixtures import REM_SLEEP
from tests.providers.garmindb.fixtures import STAGES
from tests.providers.garmindb.fixtures import Fixture
from tests.providers.garmindb.fixtures import build_fixture
from tests.providers.garmindb.fixtures import heart_rate_at
from tests.providers.garmindb.fixtures import hrv_at
from tests.providers.garmindb.fixtures import mean

HOUR = dt.timedelta(hours=1)


@pytest.fixture(autouse=True)
def _fresh_vocabulary_warnings() -> None:
    reset_unknown_event_log()


def sessions_for(settings: GarminDbSettings, **kwargs: Any) -> tuple[Fixture, list[SleepSession]]:
    """Build a corpus and serve every session in it."""
    fixture = build_fixture(settings.health_data_dir, **kwargs)
    with GarminConnection(settings) as conn:
        return fixture, build_sleep_sessions(conn, None, None, None)


class TestTimezoneConsistency:
    """The single set of tests that proves the whole timezone strategy."""

    def test_the_same_night_is_the_same_instant_whatever_tz_imported_it(
        self, tmp_path: Path
    ) -> None:
        """sleep.start is rendered in the importing container's TZ, sleep_events in
        device-local. At TZ=UTC those disagree by hours; the served instant must
        not."""
        results = []
        for index, import_tz in enumerate([dt.UTC, None]):
            settings = GarminDbSettings(
                app_data_dir=tmp_path / f"appdata{index}", home_tz=HOME_TZ_NAME
            )
            kwargs = {"import_tz": import_tz} if import_tz else {}
            fixture, sessions = sessions_for(settings, nights=3, **kwargs)
            results.append((fixture, sessions))

        (utc_fixture, utc_sessions), (home_fixture, home_sessions) = results
        assert [s.start for s in utc_sessions] == [s.start for s in home_sessions]
        assert [s.end for s in utc_sessions] == [s.end for s in home_sessions]
        assert utc_sessions[0].start == utc_fixture.newest.start_utc
        assert home_sessions[0].start == home_fixture.newest.start_utc

    def test_it_holds_with_no_events_when_the_import_zone_is_configured(
        self, tmp_path: Path
    ) -> None:
        """With no events there is nothing to learn the skew from, which is exactly
        when GARMIN_IMPORT_TZ has to be set. The window then comes from
        sleep.start/end through sleep_column_to_utc."""
        settings = GarminDbSettings(
            app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME, import_tz="UTC"
        )
        fixture, sessions = sessions_for(settings, nights=1, import_tz=dt.UTC, with_events=False)
        assert sessions[0].start == fixture.newest.start_utc
        assert sessions[0].end == fixture.newest.end_utc

    def test_every_emitted_instant_is_aware_utc(self, corpus_settings: GarminDbSettings) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1, heart_rate=True, hrv=True)
        session = sessions[0]
        assert session.start.tzinfo == dt.UTC
        assert session.end.tzinfo == dt.UTC
        assert session.stages is not None
        assert all(s.timestamp.tzinfo == dt.UTC for s in session.stages.samples)
        assert all(s.end_timestamp.tzinfo == dt.UTC for s in session.stages.samples)


class TestIdentity:
    def test_the_id_is_namespaced_and_derived_from_the_day_column(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Derived from day, not start/end: day is the primary key and is stable
        across re-imports, whereas start/end are the tz-suspect columns. Correcting
        import_offset later must not change every id a consumer has stored."""
        fixture, sessions = sessions_for(corpus_settings, nights=3)
        assert [s.id for s in sessions] == [n.session_id for n in reversed(fixture.nights)]
        assert sessions[0].id.startswith("garmin:sleep:")

    def test_the_id_does_not_move_when_the_import_zone_does(self, tmp_path: Path) -> None:
        ids = []
        for index, import_tz in enumerate([dt.UTC, None]):
            settings = GarminDbSettings(app_data_dir=tmp_path / f"a{index}", home_tz=HOME_TZ_NAME)
            kwargs = {"import_tz": import_tz} if import_tz else {}
            _, sessions = sessions_for(settings, nights=2, **kwargs)
            ids.append([s.id for s in sessions])
        assert ids[0] == ids[1]

    def test_the_source_is_the_vendor_not_our_ingest_tool(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Matching the spec's own "oura"/"apple_watch" examples. Not "garmindb"
        (our ingest tool, an implementation detail) and not "garmin_connect" (data
        can arrive from FIT files off the watch)."""
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].source == "garmin"


class TestWindowResolution:
    def test_events_are_preferred_even_when_start_and_end_are_present(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Events share a clock with monitoring_hr, so the window and its
        sub-series are guaranteed self-consistent. That consistency is the whole
        reason for preferring them."""
        fixture, sessions = sessions_for(corpus_settings, nights=1, import_tz=dt.UTC)
        assert sessions[0].start == fixture.newest.start_utc
        assert sessions[0].end == fixture.newest.end_utc

    def test_it_falls_back_to_both_columns_when_there_are_no_events(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture, sessions = sessions_for(corpus_settings, nights=1, with_events=False)
        assert sessions[0].start == fixture.newest.start_utc
        assert sessions[0].end == fixture.newest.end_utc
        assert sessions[0].stages is None

    def test_a_missing_end_is_derived_from_total_sleep_plus_awake(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture, sessions = sessions_for(
            corpus_settings, nights=1, with_events=False, null_end=True
        )
        assert len(sessions) == 1
        assert sessions[0].start == fixture.newest.start_utc
        assert sessions[0].end == fixture.newest.start_utc + dt.timedelta(hours=8)

    def test_a_missing_start_is_derived_backwards_from_the_end(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture, sessions = sessions_for(
            corpus_settings, nights=1, with_events=False, null_start=True
        )
        assert len(sessions) == 1
        assert sessions[0].end == fixture.newest.end_utc
        assert sessions[0].start == fixture.newest.end_utc - dt.timedelta(hours=8)

    def test_a_session_with_nothing_to_go_on_is_skipped_with_a_warning(
        self, corpus_settings: GarminDbSettings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Skipping over fabricating: there is no honest default for a sleep
        window, and a synthesized one looks valid, merges into
        get_sleep_sessions_merged, and silently poisons downstream aggregates. A
        gap in the list is detectable; a plausible lie is not."""
        with caplog.at_level(logging.WARNING, logger="garmin_health.providers.garmindb.sleep"):
            _, sessions = sessions_for(
                corpus_settings,
                nights=1,
                with_events=False,
                null_start=True,
                null_end=True,
                total_sleep=dt.time.min,
                stage_durations=False,
            )
        assert sessions == []
        assert "skipping" in caplog.text.lower()

    def test_a_single_column_with_no_duration_is_not_enough(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(
            corpus_settings,
            nights=1,
            with_events=False,
            null_end=True,
            total_sleep=dt.time.min,
            stage_durations=False,
        )
        assert sessions == []

    def test_a_night_is_matched_to_its_own_events_not_a_neighbours(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """A +/-24h event search around `day` catches three nights' events. The
        clustering is what keeps them apart."""
        fixture, sessions = sessions_for(corpus_settings, nights=3)
        for session, night in zip(sessions, reversed(fixture.nights), strict=True):
            assert session.start == night.start_utc
            assert session.end == night.end_utc


class TestStages:
    def test_a_night_becomes_contiguous_non_overlapping_intervals(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        stages = sessions[0].stages
        assert stages is not None
        assert len(stages.samples) == len(STAGES)
        assert stages.source == "garmin"
        for earlier, later in zip(stages.samples, stages.samples[1:], strict=False):
            assert earlier.end_timestamp == later.timestamp

    def test_every_interval_is_an_interval_sample(self, corpus_settings: GarminDbSettings) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].stages is not None
        assert all(isinstance(s, IntervalSample) for s in sessions[0].stages.samples)

    def test_the_stage_values_follow_the_fit_vocabulary(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].stages is not None
        assert [s.value for s in sessions[0].stages.samples] == [
            SleepStage.LIGHT,
            SleepStage.DEEP,
            SleepStage.DEEP,
            SleepStage.AWAKE,
            SleepStage.LIGHT,
            SleepStage.REM,
            SleepStage.REM,
            SleepStage.LIGHT,
        ]

    def test_the_non_rem_json_vocabulary_maps_too(self, corpus_settings: GarminDbSettings) -> None:
        """more_awake is a token only a non-REM device's JSON produces."""
        _, sessions = sessions_for(corpus_settings, nights=1, stages=JSON_NON_REM_STAGES)
        assert sessions[0].stages is not None
        values = [s.value for s in sessions[0].stages.samples]
        assert SleepStage.AWAKE in values
        assert SleepStage.REM not in values

    def test_an_unknown_token_degrades_and_warns_once(
        self, corpus_settings: GarminDbSettings, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="garmin_health.providers.garmindb.vocabulary"):
            _, sessions = sessions_for(corpus_settings, nights=1, stages=["banana"] * len(STAGES))
        assert sessions[0].stages is not None
        assert {s.value for s in sessions[0].stages.samples} == {SleepStage.UNKNOWN}
        assert len([r for r in caplog.records if "banana" in r.getMessage()]) == 1

    def test_overlaps_are_clamped_to_the_successors_start(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """For the FIT path duration is literally next_ts - this_ts, so an overlap
        means a clock adjustment or a duplicate row. A monotone timeline is what
        any consumer summing stage durations needs."""
        _, sessions = sessions_for(
            corpus_settings, nights=1, event_overlap=dt.timedelta(minutes=30)
        )
        stages = sessions[0].stages
        assert stages is not None
        for earlier, later in zip(stages.samples, stages.samples[1:], strict=False):
            assert earlier.end_timestamp <= later.timestamp
        assert stages.samples[0].end_timestamp == stages.samples[1].timestamp

    def test_the_final_interval_keeps_its_full_recorded_duration(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """It has no successor to be clamped against, so it is Garmin's claim."""
        _, sessions = sessions_for(
            corpus_settings, nights=1, event_overlap=dt.timedelta(minutes=30)
        )
        stages = sessions[0].stages
        assert stages is not None
        assert stages.samples[-1].end_timestamp - stages.samples[-1].timestamp == dt.timedelta(
            minutes=90
        )

    def test_zero_length_rows_are_dropped(self, corpus_settings: GarminDbSettings) -> None:
        """duration is NOT NULL DEFAULT time.min, so a zero is 'never recorded'."""
        _, sessions = sessions_for(corpus_settings, nights=1, event_overlap=-HOUR, with_events=True)
        assert sessions == [] or sessions[0].stages is None

    def test_gaps_are_left_alone_by_default(self, corpus_settings: GarminDbSettings) -> None:
        """A gap means Garmin recorded nothing there; UNKNOWN filler would be
        inventing data."""
        _, sessions = sessions_for(
            corpus_settings, nights=1, event_overlap=-dt.timedelta(minutes=30)
        )
        stages = sessions[0].stages
        assert stages is not None
        assert stages.samples[0].end_timestamp < stages.samples[1].timestamp
        assert SleepStage.UNKNOWN not in {s.value for s in stages.samples}

    def test_gaps_can_be_filled_on_request(self, tmp_path: Path) -> None:
        settings = GarminDbSettings(
            app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME, fill_stage_gaps=True
        )
        _, sessions = sessions_for(settings, nights=1, event_overlap=-dt.timedelta(minutes=30))
        stages = sessions[0].stages
        assert stages is not None
        for earlier, later in zip(stages.samples, stages.samples[1:], strict=False):
            assert earlier.end_timestamp == later.timestamp
        assert SleepStage.UNKNOWN in {s.value for s in stages.samples}

    def test_no_intervals_means_none_rather_than_an_empty_stages_object(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """They are different claims: 'no stage data' versus 'a night with no
        stages in it'."""
        _, sessions = sessions_for(corpus_settings, nights=1, with_events=False)
        assert sessions[0].stages is None


class TestDurationScalars:
    def test_the_five_columns_are_emitted_in_minutes(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        session = sessions[0]
        assert session.total_duration is not None
        assert session.total_duration.value == 420.0
        assert session.total_duration.unit == "min"
        assert session.deep_sleep_duration is not None
        assert session.deep_sleep_duration.value == DEEP_SLEEP.hour * 60.0
        assert session.light_sleep_duration is not None
        assert session.light_sleep_duration.value == LIGHT_SLEEP.hour * 60.0
        assert session.rem_sleep_duration is not None
        assert session.rem_sleep_duration.value == REM_SLEEP.hour * 60.0
        assert session.awake_time is not None
        assert session.awake_time.value == AWAKE.hour * 60.0

    def test_a_zero_total_is_recomputed_from_the_stage_intervals(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """All five columns are NOT NULL DEFAULT time.min, so 'no data' and 'zero
        minutes' are indistinguishable. A zero total is read as absent."""
        _, sessions = sessions_for(
            corpus_settings, nights=1, total_sleep=dt.time.min, stage_durations=False
        )
        session = sessions[0]
        assert session.total_duration is not None
        assert session.total_duration.value == 420.0
        assert session.deep_sleep_duration is not None
        assert session.deep_sleep_duration.value == 120.0
        assert session.awake_time is not None
        assert session.awake_time.value == 60.0

    def test_a_zero_total_with_no_events_leaves_every_duration_none(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """The window is still known -- both columns are present -- so the session
        is served with a real start, end and time_in_bed, and no breakdown. There
        is nothing to recompute from, and zero is not an honest stand-in."""
        _, sessions = sessions_for(
            corpus_settings,
            nights=1,
            with_events=False,
            total_sleep=dt.time.min,
            stage_durations=False,
        )
        session = sessions[0]
        assert session.total_duration is None
        assert session.deep_sleep_duration is None
        assert session.light_sleep_duration is None
        assert session.rem_sleep_duration is None
        assert session.awake_time is None
        assert session.time_in_bed is not None
        assert session.time_in_bed.value == 480.0
        assert session.efficiency is None

    def test_a_zero_stage_is_real_data_when_the_total_is_positive(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """A night with genuinely no REM. The asymmetry with the zero-total case is
        deliberate: a positive total means the row was populated."""
        _, sessions = sessions_for(corpus_settings, nights=1, stage_durations=False)
        session = sessions[0]
        assert session.total_duration is not None
        assert session.total_duration.value == 420.0
        assert session.rem_sleep_duration is not None
        assert session.rem_sleep_duration.value == 0.0


class TestDerivedScalars:
    def test_time_in_bed_is_the_detected_sleep_window(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Garmin's detected window IS the in-bed span, so this is shorter than
        true bed time by the pre-sleep reading period."""
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].time_in_bed is not None
        assert sessions[0].time_in_bed.value == 480.0

    def test_efficiency_is_sleep_over_time_in_bed(self, corpus_settings: GarminDbSettings) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].efficiency is not None
        assert sessions[0].efficiency.value == pytest.approx(87.5)
        assert sessions[0].efficiency.unit == "%"

    def test_an_impossible_efficiency_is_clamped_and_logged(
        self, corpus_settings: GarminDbSettings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Over 100% is precisely the canary that total_sleep and the event-derived
        window disagree, i.e. the tz skew is still present. Log it, don't swallow
        it."""
        with caplog.at_level(logging.WARNING, logger="garmin_health.providers.garmindb.sleep"):
            _, sessions = sessions_for(corpus_settings, nights=1, total_sleep=dt.time(12, 0))
        assert sessions[0].efficiency is not None
        assert sessions[0].efficiency.value == 100.0
        assert "efficiency" in caplog.text.lower()

    def test_latency_is_never_derived(self, corpus_settings: GarminDbSettings) -> None:
        """Latency is lights-out to sleep onset, and GarminDB has no lights-out
        marker: sleep.start IS the detected onset and the first event is already a
        sleep stage, so any derivation is structurally 0 or noise."""
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].latency is None

    def test_restless_periods_are_not_derived_by_default(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Oura's restless_periods is a movement-derived count, not an awakening
        count, and Count carries no unit or qualifier that would let a consumer
        tell them apart in a merged list."""
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].restless_periods is None

    def test_restless_periods_count_interior_awakenings_when_opted_in(self, tmp_path: Path) -> None:
        settings = GarminDbSettings(
            app_data_dir=tmp_path / "appdata",
            home_tz=HOME_TZ_NAME,
            derive_restless_periods=True,
        )
        _, sessions = sessions_for(settings, nights=1)
        assert sessions[0].restless_periods is not None
        assert sessions[0].restless_periods.value == 1
        assert type(sessions[0].restless_periods.value) is int

    def test_an_awakening_at_the_very_end_is_not_interior(self, tmp_path: Path) -> None:
        """Waking up is how a night ends; it is not a restless period within it."""
        settings = GarminDbSettings(
            app_data_dir=tmp_path / "appdata",
            home_tz=HOME_TZ_NAME,
            derive_restless_periods=True,
        )
        stages = ["light_sleep"] * 7 + ["awake"]
        _, sessions = sessions_for(settings, nights=1, stages=stages)
        assert sessions[0].restless_periods is not None
        assert sessions[0].restless_periods.value == 0


class TestSessionSubSeries:
    def test_heart_rate_and_hrv_are_sliced_to_the_night(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1, heart_rate=True, hrv=True)
        session = sessions[0]
        assert session.heart_rate is not None
        assert len(session.heart_rate.samples) == 240
        assert session.hrv is not None
        assert len(session.hrv.samples) == HRV_ROWS
        assert session.hrv.unit == "ms"

    def test_absent_sub_series_are_none(self, corpus_settings: GarminDbSettings) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].heart_rate is None
        assert sessions[0].hrv is None


class TestSessionScalars:
    def test_heart_rate_summaries_come_from_the_window(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1, heart_rate=True)
        session = sessions[0]
        expected = [float(heart_rate_at(i)) for i in range(240)]
        assert session.average_heart_rate is not None
        assert session.average_heart_rate.value == pytest.approx(mean(expected))
        assert session.lowest_heart_rate is not None
        assert session.lowest_heart_rate.value == pytest.approx(min(expected))

    def test_the_hrv_average_agrees_with_the_hrv_sub_series(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1, hrv=True)
        session = sessions[0]
        assert session.average_hrv is not None
        assert session.hrv is not None
        assert session.average_hrv.value == pytest.approx(
            mean([s.value for s in session.hrv.samples])
        )
        assert session.average_hrv.value == pytest.approx(
            mean([hrv_at(i) for i in range(HRV_ROWS)])
        )

    def test_it_falls_back_to_the_fit_hrv_status_table(
        self, corpus_settings: GarminDbSettings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """monitoring_hrv_* is FIT-only and hrv is JSON-only; neither is
        universally present."""
        with caplog.at_level(logging.INFO, logger="garmin_health.providers.garmindb.sleep"):
            _, sessions = sessions_for(corpus_settings, nights=1, hrv_status=True)
        assert sessions[0].average_hrv is not None
        assert sessions[0].average_hrv.value == 44.0
        assert "hrv" in caplog.text.lower()

    def test_it_falls_back_to_the_json_daily_hrv_table_last(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1, daily_hrv=True)
        assert sessions[0].average_hrv is not None
        assert sessions[0].average_hrv.value == 38.0

    def test_no_hrv_anywhere_means_none(self, corpus_settings: GarminDbSettings) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].average_hrv is None

    def test_breath_rate_prefers_the_sleep_column(self, corpus_settings: GarminDbSettings) -> None:
        """Deliberately the opposite preference to HRV, and that is fine BECAUSE
        SleepSession has no respiration sub-series for it to disagree with."""
        _, sessions = sessions_for(corpus_settings, nights=1, avg_rr=14.5, respiration=True)
        assert sessions[0].average_breath is not None
        assert sessions[0].average_breath.value == pytest.approx(14.5)

    def test_breath_rate_falls_back_to_the_monitoring_window(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1, respiration=True)
        assert sessions[0].average_breath is not None
        assert sessions[0].average_breath.value == pytest.approx(13.375)

    def test_the_sleep_score_is_a_unitless_zero_to_one_hundred_score(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1, sleep_score=82)
        assert sessions[0].sleep_score is not None
        assert sessions[0].sleep_score.value == 82.0
        assert sessions[0].sleep_score.unit is None

    def test_an_unscored_night_has_no_score(self, corpus_settings: GarminDbSettings) -> None:
        _, sessions = sessions_for(corpus_settings, nights=1)
        assert sessions[0].sleep_score is None


class TestOrderingAndWindow:
    def test_sessions_are_newest_first(self, corpus_settings: GarminDbSettings) -> None:
        """Matching get_sleep_sessions_merged, which sorts by start descending."""
        _, sessions = sessions_for(corpus_settings, nights=3)
        starts = [s.start for s in sessions]
        assert starts == sorted(starts, reverse=True)

    def test_the_limit_keeps_the_most_recent_nights(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """Unlike a time series, the client DOES re-apply limit to sleep sessions,
        so newest-first truncation is what it expects."""
        fixture = build_fixture(corpus_settings.health_data_dir, nights=5)
        with GarminConnection(corpus_settings) as conn:
            sessions = build_sleep_sessions(conn, None, None, 2)
        assert [s.id for s in sessions] == [
            fixture.nights[-1].session_id,
            fixture.nights[-2].session_id,
        ]

    def test_only_sessions_starting_inside_the_window_are_served(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture = build_fixture(corpus_settings.health_data_dir, nights=5)
        start = fixture.nights[-2].start_utc
        with GarminConnection(corpus_settings) as conn:
            sessions = build_sleep_sessions(conn, start, None, None)
        assert [s.id for s in sessions] == [
            fixture.nights[-1].session_id,
            fixture.nights[-2].session_id,
        ]

    def test_the_window_is_half_open_at_the_end(self, corpus_settings: GarminDbSettings) -> None:
        fixture = build_fixture(corpus_settings.health_data_dir, nights=3)
        with GarminConnection(corpus_settings) as conn:
            sessions = build_sleep_sessions(conn, None, fixture.nights[-1].start_utc, None)
        assert fixture.nights[-1].session_id not in {s.id for s in sessions}
        assert len(sessions) == 2

    def test_an_empty_corpus_yields_an_empty_list(self, corpus_settings: GarminDbSettings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=0)
        with GarminConnection(corpus_settings) as conn:
            assert build_sleep_sessions(conn, None, None, None) == []


def test_a_session_carries_only_fields_the_spec_declares(
    corpus_settings: GarminDbSettings,
) -> None:
    """attrs would accept a typo'd keyword nowhere, but a field we quietly stop
    populating would go unnoticed. This pins the full set we fill in."""
    _, sessions = sessions_for(
        corpus_settings,
        nights=1,
        heart_rate=True,
        hrv=True,
        sleep_score=82,
        avg_rr=14.5,
    )
    populated = {f.name for f in attrs.fields(SleepSession) if getattr(sessions[0], f.name)}
    assert populated == {
        "start",
        "end",
        "id",
        "source",
        "stages",
        "heart_rate",
        "hrv",
        "total_duration",
        "deep_sleep_duration",
        "light_sleep_duration",
        "rem_sleep_duration",
        "awake_time",
        "time_in_bed",
        "average_heart_rate",
        "lowest_heart_rate",
        "average_hrv",
        "average_breath",
        "efficiency",
        "sleep_score",
    }


class TestBoundedWork:
    """A consumer asking for 60 sessions must not cost a session per night held.

    `get_sleep_sessions_merged` sends `limit` and **no window**, so an unbounded
    request over a multi-year corpus is the normal case, not an edge case. Each
    session costs several monitoring queries (heart-rate slice, HRV slice, the
    stats, the respiration average), so building every night and then keeping 60
    is thousands of queries the consumer's 30s httpx timeout will not wait for.
    """

    def test_only_the_requested_number_of_sessions_is_built(
        self, corpus_settings: GarminDbSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=10)
        built: list[str] = []
        real = sleep_module._build_one

        def counting(row: Any, *args: Any, **kwargs: Any) -> Any:
            built.append(str(row.day))
            return real(row, *args, **kwargs)

        monkeypatch.setattr(sleep_module, "_build_one", counting)
        with GarminConnection(corpus_settings) as conn:
            sessions = build_sleep_sessions(conn, None, None, 2)

        assert len(sessions) == 2
        assert len(built) == 2, f"built {len(built)} sessions to return 2"

    def test_the_newest_nights_are_the_ones_built(self, corpus_settings: GarminDbSettings) -> None:
        """Sessions are served newest-first, so stopping early has to stop at the
        *old* end or the limit would return the wrong nights."""
        fixture = build_fixture(corpus_settings.health_data_dir, nights=10)
        with GarminConnection(corpus_settings) as conn:
            sessions = build_sleep_sessions(conn, None, None, 3)
        assert [s.id for s in sessions] == [n.session_id for n in fixture.nights[-3:][::-1]]

    def test_a_full_request_still_returns_everything(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=5)
        with GarminConnection(corpus_settings) as conn:
            assert len(build_sleep_sessions(conn, None, None, 50)) == 5


class TestEventsBelongToTheirOwnNight:
    """A night's stages must come from that night's events, never a neighbour's."""

    @staticmethod
    def _drop_events_for(fixture: Fixture, night: Any) -> None:
        with fixture.garmin_db.managed_session() as session:
            session.query(SleepEvents).filter(
                SleepEvents.timestamp >= night.first_event_local - dt.timedelta(hours=1)
            ).delete(synchronize_session=False)

    def test_a_night_without_events_does_not_borrow_the_previous_nights(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        """With no window the candidate clusters span the whole corpus, so a
        nearest-cluster match with no distance cap silently hands this night the
        previous night's window and stage timeline."""
        fixture = build_fixture(corpus_settings.health_data_dir, nights=3)
        newest = fixture.newest
        self._drop_events_for(fixture, newest)

        with GarminConnection(corpus_settings) as conn:
            sessions = build_sleep_sessions(conn, None, None, None)

        served = next(s for s in sessions if s.id == newest.session_id)
        assert served.start == newest.start_utc
        assert served.end == newest.end_utc
        assert served.stages is None

    def test_the_other_nights_keep_their_own_stages(
        self, corpus_settings: GarminDbSettings
    ) -> None:
        fixture = build_fixture(corpus_settings.health_data_dir, nights=3)
        self._drop_events_for(fixture, fixture.newest)

        with GarminConnection(corpus_settings) as conn:
            sessions = build_sleep_sessions(conn, None, None, None)

        for session, night in zip(sessions[1:], fixture.nights[:-1][::-1], strict=True):
            assert session.start == night.start_utc
            assert session.stages is not None
            assert len(session.stages.samples) == len(STAGES)
