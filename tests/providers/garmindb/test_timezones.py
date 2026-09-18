"""Tests for the pure timezone policy.

This is the highest-risk area of the project: every failure mode here is a silent
wrong answer rather than an exception, so most of these tests exist to pin down
behaviour that would otherwise drift unnoticed.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from garmin_health.providers.garmindb.timezones import TimeZonePolicy
from garmin_health.providers.garmindb.timezones import TimeZoneUnresolved
from garmin_health.providers.garmindb.timezones import learn_import_offset
from garmin_health.providers.garmindb.timezones import resolve_home_tz

DENVER = ZoneInfo("America/Denver")
UTC = dt.UTC

# 23:00 on 14 June, Denver (MDT, UTC-6) == 05:00Z on 15 June.
SUMMER_LOCAL = dt.datetime(2026, 6, 14, 23, 0)
SUMMER_UTC = dt.datetime(2026, 6, 15, 5, 0, tzinfo=UTC)
# 23:00 on 14 January, Denver (MST, UTC-7) == 06:00Z on 15 January.
WINTER_LOCAL = dt.datetime(2026, 1, 14, 23, 0)
WINTER_UTC = dt.datetime(2026, 1, 15, 6, 0, tzinfo=UTC)


@pytest.fixture
def policy() -> TimeZonePolicy:
    return TimeZonePolicy(home_tz=DENVER)


class TestToUtc:
    def test_converts_device_local_in_summer(self, policy: TimeZonePolicy) -> None:
        assert policy.to_utc(SUMMER_LOCAL) == SUMMER_UTC

    def test_converts_device_local_in_winter(self, policy: TimeZonePolicy) -> None:
        """The same wall clock is a different instant either side of a DST change."""
        assert policy.to_utc(WINTER_LOCAL) == WINTER_UTC

    def test_result_is_always_aware_utc(self, policy: TimeZonePolicy) -> None:
        """isoformat() on a naive value emits an offsetless string, and the spec's
        client would hand the consumer a naive datetime."""
        result = policy.to_utc(SUMMER_LOCAL)
        assert result.tzinfo is dt.UTC
        assert result.isoformat().endswith("+00:00")

    def test_rejects_an_aware_input(self, policy: TimeZonePolicy) -> None:
        """.replace(tzinfo=home) on an aware value silently relabels it, moving the
        instant by hours with no error."""
        with pytest.raises(ValueError, match="naive"):
            policy.to_utc(SUMMER_UTC)

    def test_ignores_the_import_offset(self) -> None:
        """Scope guarantee: the import skew belongs to sleep.start/sleep.end alone.
        Applying it here would shift monitoring_hr, sleep_events and every *.day
        column by hours -- the whole corpus, silently."""
        skewed = TimeZonePolicy(home_tz=DENVER, import_offset=dt.timedelta(hours=6))
        assert skewed.to_utc(SUMMER_LOCAL) == SUMMER_UTC

    def test_ignores_the_import_zone(self) -> None:
        skewed = TimeZonePolicy(home_tz=DENVER, import_tz=UTC)
        assert skewed.to_utc(SUMMER_LOCAL) == SUMMER_UTC


class TestToNaiveLocal:
    def test_is_the_inverse_of_to_utc(self, policy: TimeZonePolicy) -> None:
        assert policy.to_naive_local(policy.to_utc(SUMMER_LOCAL)) == SUMMER_LOCAL

    def test_returns_a_naive_bound(self, policy: TimeZonePolicy) -> None:
        """SQLAlchemy's SQLite DATETIME bind processor formats .year/.hour/... and
        DISCARDS tzinfo, so an aware bound silently compares as a naive wall clock
        against naive local rows -- wrong rows, no error."""
        assert policy.to_naive_local(SUMMER_UTC).tzinfo is None

    def test_converts_the_instant_not_just_the_label(self, policy: TimeZonePolicy) -> None:
        assert policy.to_naive_local(SUMMER_UTC) == SUMMER_LOCAL
        assert policy.to_naive_local(WINTER_UTC) == WINTER_LOCAL

    def test_rejects_a_naive_input(self, policy: TimeZonePolicy) -> None:
        """datetime.astimezone() on a naive value assumes the *system* zone. On a
        container left at TZ=UTC that is a silently different answer than on a
        developer's laptop, which is exactly the bug class this module exists for."""
        with pytest.raises(ValueError, match="aware"):
            policy.to_naive_local(SUMMER_LOCAL)

    def test_ignores_the_import_offset(self) -> None:
        """Query bounds address rows on the device clock, never the importer's."""
        skewed = TimeZonePolicy(home_tz=DENVER, import_offset=dt.timedelta(hours=6))
        assert skewed.to_naive_local(SUMMER_UTC) == SUMMER_LOCAL


class TestDaylightSavingEdges:
    """Two nights a year, on data Garmin itself recorded ambiguously. The point is
    not that these answers are the only defensible ones -- it is that they are
    pinned, so a Python or tzdata upgrade cannot change them silently."""

    def test_ambiguous_autumn_hour_resolves_to_the_first_pass(self, policy: TimeZonePolicy) -> None:
        # 01:30 happens twice on 1 Nov 2026. fold=0 picks the DST (-06:00) pass.
        ambiguous = dt.datetime(2026, 11, 1, 1, 30)
        assert policy.to_utc(ambiguous) == dt.datetime(2026, 11, 1, 7, 30, tzinfo=UTC)

    def test_nonexistent_spring_hour_uses_the_pre_transition_offset(
        self, policy: TimeZonePolicy
    ) -> None:
        # 02:30 never happens on 8 Mar 2026; it is read as MST (-07:00).
        nonexistent = dt.datetime(2026, 3, 8, 2, 30)
        assert policy.to_utc(nonexistent) == dt.datetime(2026, 3, 8, 9, 30, tzinfo=UTC)

    def test_a_night_spanning_the_autumn_change_is_nine_hours(self, policy: TimeZonePolicy) -> None:
        """23:00 -> 07:00 on the wall clock is a real nine hours when the clocks go
        back, which is why wall-clock arithmetic must never stand in for instants."""
        start = policy.to_utc(dt.datetime(2026, 10, 31, 23, 0))
        end = policy.to_utc(dt.datetime(2026, 11, 1, 7, 0))
        assert end - start == dt.timedelta(hours=9)


class TestSleepColumnToUtc:
    """sleep.start/sleep.end are the only two columns rendered in the importing
    container's TZ rather than device-local."""

    def test_repairs_a_utc_imported_column(self) -> None:
        # Imported under TZ=UTC: the naive value is 05:00 while every other table
        # says 23:00. offset = import_utcoffset - home_utcoffset = 0 - (-6) = 6h.
        policy = TimeZonePolicy(home_tz=DENVER, import_offset=dt.timedelta(hours=6))
        assert policy.sleep_column_to_utc(dt.datetime(2026, 6, 15, 5, 0)) == SUMMER_UTC

    def test_is_a_no_op_when_the_importer_ran_in_the_home_zone(
        self, policy: TimeZonePolicy
    ) -> None:
        assert policy.sleep_column_to_utc(SUMMER_LOCAL) == SUMMER_UTC

    def test_rejects_an_aware_input(self, policy: TimeZonePolicy) -> None:
        with pytest.raises(ValueError, match="naive"):
            policy.sleep_column_to_utc(SUMMER_UTC)

    def test_a_known_import_zone_is_exact_across_a_dst_change(self) -> None:
        """A learned scalar offset is a single number, so it can only be right for
        one side of a DST transition when the import zone and the home zone change
        clocks on different dates. Naming the import zone removes the guess."""
        policy = TimeZonePolicy(home_tz=DENVER, import_tz=UTC)
        assert policy.sleep_column_to_utc(dt.datetime(2026, 6, 15, 5, 0)) == SUMMER_UTC
        assert policy.sleep_column_to_utc(dt.datetime(2026, 1, 15, 6, 0)) == WINTER_UTC

    def test_a_scalar_offset_is_wrong_on_the_other_side_of_a_dst_change(self) -> None:
        """Pinned to document the limitation the import-zone override exists to fix:
        6h is learned in summer, but the winter answer is then an hour out."""
        scalar = TimeZonePolicy(home_tz=DENVER, import_offset=dt.timedelta(hours=6))
        exact = TimeZonePolicy(home_tz=DENVER, import_tz=UTC)
        winter_column = dt.datetime(2026, 1, 15, 6, 0)
        assert exact.sleep_column_to_utc(winter_column) == WINTER_UTC
        assert scalar.sleep_column_to_utc(winter_column) - WINTER_UTC == dt.timedelta(hours=1)

    def test_the_import_zone_wins_over_a_learned_offset(self) -> None:
        policy = TimeZonePolicy(home_tz=DENVER, import_offset=dt.timedelta(hours=3), import_tz=UTC)
        assert policy.sleep_column_to_utc(dt.datetime(2026, 6, 15, 5, 0)) == SUMMER_UTC


class TestLearnImportOffset:
    """sleep.start and the first sleep_events row of a night describe the same
    instant on two different clocks, so their naive difference is the offset."""

    @staticmethod
    def pair(delta_hours: float) -> tuple[dt.datetime, dt.datetime]:
        event = dt.datetime(2026, 6, 14, 23, 0)
        return event + dt.timedelta(hours=delta_hours), event

    def test_learns_the_offset_from_clean_pairs(self) -> None:
        assert learn_import_offset([self.pair(6)] * 3) == dt.timedelta(hours=6)

    def test_takes_the_mode_rather_than_the_mean(self) -> None:
        """One mispaired night must not drag the answer off a real UTC offset."""
        pairs = [self.pair(6), self.pair(6), self.pair(6), self.pair(-2)]
        assert learn_import_offset(pairs) == dt.timedelta(hours=6)

    def test_learns_a_negative_offset(self) -> None:
        assert learn_import_offset([self.pair(-9)] * 2) == dt.timedelta(hours=-9)

    def test_learns_a_fractional_offset(self) -> None:
        """India is +05:30 and Nepal +05:45; offsets are not whole hours."""
        assert learn_import_offset([self.pair(5.5)] * 2) == dt.timedelta(hours=5, minutes=30)

    def test_discards_deltas_that_are_not_a_whole_quarter_hour(self) -> None:
        """Every real UTC offset is a multiple of 15 minutes, so anything else is a
        first event that simply was not at sleep.start."""
        assert learn_import_offset([self.pair(6.1), self.pair(5.9)]) is None

    def test_discards_implausible_deltas(self) -> None:
        """The whole span of real UTC offsets is -12..+14, so a difference of two
        of them cannot exceed 26 hours."""
        assert learn_import_offset([self.pair(30)] * 3) is None

    def test_returns_none_with_no_pairs(self) -> None:
        assert learn_import_offset([]) is None

    def test_zero_is_a_real_answer_not_a_missing_one(self) -> None:
        """The steady state -- container TZ set to the home zone -- learns 0, which
        must be distinguishable from 'could not learn'."""
        assert learn_import_offset([self.pair(0)] * 2) == dt.timedelta(0)

    def test_ties_break_towards_the_smaller_offset(self) -> None:
        """Deterministic so a reordering of rows cannot change the answer."""
        pairs = [self.pair(7), self.pair(6)]
        assert learn_import_offset(pairs) == dt.timedelta(hours=6)
        assert learn_import_offset(list(reversed(pairs))) == dt.timedelta(hours=6)


class TestResolveHomeTz:
    def test_uses_the_configured_zone_first(self) -> None:
        assert resolve_home_tz(configured="Europe/Berlin", stored=None) == ZoneInfo("Europe/Berlin")

    def test_configured_wins_over_stored(self) -> None:
        assert resolve_home_tz(configured="Europe/Berlin", stored="America/Denver") == ZoneInfo(
            "Europe/Berlin"
        )

    def test_falls_back_to_the_zone_garmin_stored(self) -> None:
        assert resolve_home_tz(configured=None, stored="America/Denver") == DENVER

    @pytest.mark.parametrize("garbage", ["0", "TimeMode.twentyfour_hour", "us_mountain", "", "  "])
    def test_rejects_a_stored_value_that_is_not_an_iana_zone(self, garbage: str) -> None:
        """Two importers write attributes.time_zone under last-writer-wins:
        GarminPersonalInformation writes IANA, fit_file_processor writes a
        stringified FIT enum. The FIT one must never be believed."""
        with pytest.raises(TimeZoneUnresolved):
            resolve_home_tz(configured=None, stored=garbage)

    def test_raises_when_nothing_resolves(self) -> None:
        with pytest.raises(TimeZoneUnresolved, match="GARMIN_HOME_TZ"):
            resolve_home_tz(configured=None, stored=None)

    def test_never_falls_back_to_the_container_zone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A silent wrong answer here corrupts every timestamp the service emits and
        would not be noticed for months. Failing at boot is a five-second fix."""
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        with pytest.raises(TimeZoneUnresolved):
            resolve_home_tz(configured=None, stored=None)

    def test_a_bad_configured_zone_is_reported_as_such(self) -> None:
        with pytest.raises(TimeZoneUnresolved, match="GARMIN_HOME_TZ"):
            resolve_home_tz(configured="Mars/Olympus_Mons", stored="America/Denver")
