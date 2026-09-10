"""The sleep-event vocabulary: two enums from two ingest paths, one spec enum."""

from __future__ import annotations

import logging

import fitfile.field_enums
import pytest
from garmindb.import_monitoring import RemSleepActivityLevels
from garmindb.import_monitoring import SleepActivityLevels
from health_data_service import SleepStage

from garmin_health.garmin.vocabulary import known_events
from garmin_health.garmin.vocabulary import reset_unknown_event_log
from garmin_health.garmin.vocabulary import stage_for_event


@pytest.fixture(autouse=True)
def _fresh_warning_state() -> None:
    """The one-shot log is process-global, which is the point of it."""
    reset_unknown_event_log()


class TestFitVocabulary:
    """fitfile.field_enums.SleepActivityLevel -- the FIT ingest path."""

    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            ("unknown", SleepStage.UNKNOWN),
            ("awake", SleepStage.AWAKE),
            ("light_sleep", SleepStage.LIGHT),
            ("deep_sleep", SleepStage.DEEP),
            ("rem_sleep", SleepStage.REM),
        ],
    )
    def test_every_member_maps(self, event: str, expected: SleepStage) -> None:
        assert stage_for_event(event) is expected

    def test_the_enum_has_not_grown_a_member_we_do_not_map(self) -> None:
        """Reads the live enum, so a fitfile upgrade that adds a level fails here
        rather than degrading a night's stages to UNKNOWN in production."""
        names = {member.name for member in fitfile.field_enums.SleepActivityLevel}
        assert names <= known_events()


class TestJsonVocabulary:
    """garmindb.import_monitoring's two enums -- the Garmin Connect JSON path."""

    def test_a_non_rem_device_reports_more_awake(self) -> None:
        assert stage_for_event("more_awake") is SleepStage.AWAKE

    def test_a_rem_device_reports_unmeasurable(self) -> None:
        assert stage_for_event("unmeasurable") is SleepStage.UNKNOWN

    def test_neither_json_enum_has_grown_a_member_we_do_not_map(self) -> None:
        for enum_cls in (SleepActivityLevels, RemSleepActivityLevels):
            names = {member.name for member in enum_cls}
            assert names <= known_events(), enum_cls.__name__


def test_the_legacy_wake_time_token_still_maps() -> None:
    """SleepEvents.get_wake_time() still queries for it, so old rows carry it."""
    assert stage_for_event("wake_time") is SleepStage.AWAKE


class TestUnknownTokens:
    def test_an_unmapped_token_degrades_to_unknown(self) -> None:
        assert stage_for_event("banana") is SleepStage.UNKNOWN

    def test_an_unmapped_token_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="garmin_health.garmin.vocabulary"):
            stage_for_event("banana")
        assert "banana" in caplog.text

    def test_it_warns_once_per_token_not_once_per_row(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A night is hundreds of events. Warning per row would bury the signal in
        its own noise, and the point of the warning is to make new Garmin
        vocabulary visible."""
        with caplog.at_level(logging.WARNING, logger="garmin_health.garmin.vocabulary"):
            for _ in range(50):
                stage_for_event("banana")
        assert len(caplog.records) == 1

    def test_a_second_distinct_token_warns_again(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="garmin_health.garmin.vocabulary"):
            stage_for_event("banana")
            stage_for_event("kumquat")
        assert len(caplog.records) == 2

    def test_a_known_token_never_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="garmin_health.garmin.vocabulary"):
            stage_for_event("deep_sleep")
        assert caplog.records == []

    def test_a_null_event_column_is_unknown_and_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """sleep_events.event is nullable, so this is reachable from real data."""
        with caplog.at_level(logging.WARNING, logger="garmin_health.garmin.vocabulary"):
            assert stage_for_event(None) is SleepStage.UNKNOWN
            assert stage_for_event(None) is SleepStage.UNKNOWN
        assert len(caplog.records) == 1


class TestNormalization:
    def test_surrounding_whitespace_and_case_do_not_lose_a_stage(self) -> None:
        assert stage_for_event("  Deep_Sleep ") is SleepStage.DEEP

    def test_normalizing_does_not_hide_a_genuinely_new_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="garmin_health.garmin.vocabulary"):
            assert stage_for_event("Micro_Nap") is SleepStage.UNKNOWN
        assert len(caplog.records) == 1
