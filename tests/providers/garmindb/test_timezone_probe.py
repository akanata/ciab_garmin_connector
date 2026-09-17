"""Tests for the GarminDB side of the timezone strategy, against a real SQLite corpus."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from garmindb.garmindb import Attributes
from garmindb.garmindb import MonitoringHeartRate

from garmin_health.providers.garmindb.timezone_probe import read_offset_pairs
from garmin_health.providers.garmindb.timezone_probe import read_stored_time_zone
from garmin_health.providers.garmindb.timezone_probe import resolve_policy
from garmin_health.timezones import TimeZoneUnresolved
from tests.providers.garmindb.fixtures import HOME_TZ
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import build_fixture

UTC = dt.UTC


class TestReadStoredTimeZone:
    def test_reads_what_garmin_stored(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path)
        assert read_stored_time_zone(fixture.garmin_db) == HOME_TZ_NAME

    def test_returns_none_when_never_written(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, stored_time_zone=None)
        assert read_stored_time_zone(fixture.garmin_db) is None

    def test_reads_the_fit_enum_garbage_verbatim_without_interpreting_it(
        self, tmp_path: Path
    ) -> None:
        """Deciding it is unusable is resolve_home_tz's job, not the reader's."""
        fixture = build_fixture(tmp_path, stored_time_zone="0")
        assert read_stored_time_zone(fixture.garmin_db) == "0"


class TestReadOffsetPairs:
    def test_pairs_each_night_with_its_own_first_event(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, import_tz=UTC, nights=3)
        pairs = read_offset_pairs(fixture.garmin_db)
        assert len(pairs) == 3
        for (start, event), night in zip(pairs, fixture.nights, strict=True):
            assert start == night.start_as_imported(UTC)
            assert event == night.first_event_local

    def test_does_not_mispair_with_the_previous_nights_events(self, tmp_path: Path) -> None:
        """Consecutive nights put another night's events within a naive day of this
        one. Anchoring the search on sleep.day works because day and sleep_events
        are both on the home clock, so the window does not move with the offset."""
        fixture = build_fixture(tmp_path, import_tz=UTC, nights=5)
        deltas = {start - event for start, event in read_offset_pairs(fixture.garmin_db)}
        assert deltas == {dt.timedelta(hours=6)}

    def test_skips_nights_with_no_events(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, with_events=False, nights=3)
        assert read_offset_pairs(fixture.garmin_db) == []

    def test_skips_nights_with_a_null_start(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, null_start=True, nights=3)
        assert read_offset_pairs(fixture.garmin_db) == []

    def test_probes_only_the_most_recent_nights(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, nights=5)
        pairs = read_offset_pairs(fixture.garmin_db, nights=2)
        assert len(pairs) == 2
        # The newest two, not the oldest two.
        assert pairs[-1][1] == fixture.newest.first_event_local

    def test_an_empty_corpus_yields_no_pairs(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, nights=0)
        assert read_offset_pairs(fixture.garmin_db) == []


class TestResolvePolicy:
    def test_learns_the_skew_of_a_utc_imported_corpus(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, import_tz=UTC, nights=5)
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=None, configured_import_tz=None
        )
        assert policy.home_tz == HOME_TZ
        assert policy.import_offset == dt.timedelta(hours=6)

    def test_learns_a_zero_skew_when_the_importer_ran_in_the_home_zone(
        self, tmp_path: Path
    ) -> None:
        fixture = build_fixture(tmp_path, import_tz=HOME_TZ, nights=5)
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=None, configured_import_tz=None
        )
        assert policy.import_offset == dt.timedelta(0)

    def test_falls_back_to_zero_skew_when_nothing_can_be_learned(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, nights=0)
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=HOME_TZ_NAME, configured_import_tz=None
        )
        assert policy.import_offset == dt.timedelta(0)

    def test_warns_when_it_cannot_learn_but_there_are_eventless_nights(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Those are exactly the nights that will go through the unlearned offset."""
        fixture = build_fixture(tmp_path, with_events=False, nights=3)
        with caplog.at_level("WARNING"):
            resolve_policy(
                fixture.garmin_db, configured_home_tz=HOME_TZ_NAME, configured_import_tz=None
            )
        assert any("import offset" in record.message.lower() for record in caplog.records)

    def test_a_configured_import_zone_skips_learning_entirely(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, import_tz=UTC, nights=5)
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=None, configured_import_tz="UTC"
        )
        assert policy.import_tz == ZoneInfo("UTC")
        assert (
            policy.sleep_column_to_utc(fixture.newest.start_as_imported(UTC))
            == fixture.newest.start_utc
        )

    def test_a_bogus_import_zone_fails_at_startup(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, nights=1)
        with pytest.raises(TimeZoneUnresolved, match="GARMIN_IMPORT_TZ"):
            resolve_policy(
                fixture.garmin_db,
                configured_home_tz=HOME_TZ_NAME,
                configured_import_tz="Mars/Olympus_Mons",
            )

    def test_raises_when_the_stored_zone_is_fit_enum_garbage(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, stored_time_zone="0")
        with pytest.raises(TimeZoneUnresolved):
            resolve_policy(fixture.garmin_db, configured_home_tz=None, configured_import_tz=None)

    def test_a_configured_home_zone_rescues_a_corpus_with_garbage(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, stored_time_zone="0")
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=HOME_TZ_NAME, configured_import_tz=None
        )
        assert policy.home_tz == HOME_TZ

    def test_a_fresh_corpus_with_no_zone_at_all_fails_at_startup(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, stored_time_zone=None, nights=0)
        with pytest.raises(TimeZoneUnresolved, match="GARMIN_HOME_TZ"):
            resolve_policy(fixture.garmin_db, configured_home_tz=None, configured_import_tz=None)


class TestTheStrategyEndToEnd:
    """The single test that proves the whole strategy."""

    @pytest.mark.parametrize("import_tz", [UTC, HOME_TZ, ZoneInfo("Asia/Tokyo")])
    def test_sleep_start_is_the_same_instant_whatever_tz_the_import_ran_under(
        self, tmp_path: Path, import_tz: dt.tzinfo
    ) -> None:
        fixture = build_fixture(tmp_path, import_tz=import_tz, nights=5)
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=None, configured_import_tz=None
        )

        newest = fixture.newest
        stored_start = newest.start_as_imported(import_tz)
        assert policy.sleep_column_to_utc(stored_start) == newest.start_utc

    def test_the_event_clock_and_the_sleep_clock_agree_after_conversion(
        self, tmp_path: Path
    ) -> None:
        """The two tables disagree by hours on disk; after conversion they must
        describe the same instant, which is what makes sub-series slicing correct."""
        fixture = build_fixture(tmp_path, import_tz=UTC, nights=3)
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=None, configured_import_tz=None
        )

        newest = fixture.newest
        from_sleep_column = policy.sleep_column_to_utc(newest.start_as_imported(UTC))
        from_events = policy.to_utc(newest.first_event_local)
        assert from_sleep_column == from_events


class TestNaiveQueryBounds:
    """Why to_naive_local exists, demonstrated against real SQLite rather than
    asserted in a comment."""

    def test_an_aware_bound_silently_selects_the_wrong_rows(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path, nights=1, heart_rate=True)
        policy = resolve_policy(
            fixture.garmin_db, configured_home_tz=HOME_TZ_NAME, configured_import_tz=None
        )
        start_utc = fixture.newest.start_utc
        end_utc = start_utc + dt.timedelta(hours=8)

        with fixture.monitoring_db.managed_session() as session:
            correct = MonitoringHeartRate.s_get_for_period(
                session, policy.to_naive_local(start_utc), policy.to_naive_local(end_utc)
            )
            # The aware bounds describe the very same instants -- SQLite just never
            # sees the offset, so they are compared as the wrong wall clock.
            aware = MonitoringHeartRate.s_get_for_period(session, start_utc, end_utc)

        assert len(correct) == 240
        # The aware bound neither errors nor returns nothing: it silently returns a
        # plausible-looking but wrong subset (here the 2h where the discarded-offset
        # wall clock happens to overlap the real rows), which is what makes this the
        # most dangerous mistake available in this codebase.
        assert 0 < len(aware) < len(correct)
        assert aware != correct

    def test_attributes_survive_the_round_trip(self, tmp_path: Path) -> None:
        fixture = build_fixture(tmp_path)
        assert Attributes.get_string(fixture.garmin_db, "time_zone") == HOME_TZ_NAME
