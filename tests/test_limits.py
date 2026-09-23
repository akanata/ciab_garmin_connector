"""The limit policy, decimation, and the scan cap.

None of this knows where the rows came from. It moved out of the GarminDB
package precisely because every provider serving a window owes a consumer the
same three answers: how many samples it will emit, which ones, and when a window
is too wide to scan at all.
"""

from __future__ import annotations

import pytest

from garmin_health import limits
from garmin_health.config import DEFAULT_LIMIT
from garmin_health.config import MAX_LIMIT
from garmin_health.config import MAX_ROWS_SCANNED
from garmin_health.limits import InvalidLimit
from garmin_health.limits import WindowTooLarge
from garmin_health.limits import check_scan_cap
from garmin_health.limits import decimate
from garmin_health.limits import resolve_limit

# A night of continuous heart rate, which is the series these rules were sized
# against. A plain number rather than the GarminDB fixture's constant: this suite
# must not import a provider.
SERIES_ROWS = 240


class TestDecimate:
    def test_a_short_series_is_returned_untouched(self) -> None:
        rows = list(range(5))
        assert decimate(rows, 10) == rows

    def test_no_limit_returns_everything(self) -> None:
        rows = list(range(5))
        assert decimate(rows, None) == rows

    def test_it_keeps_exactly_the_limit(self) -> None:
        assert len(decimate(list(range(SERIES_ROWS)), 10)) == 10

    def test_the_first_and_last_readings_are_always_kept(self) -> None:
        """The consumer asked for a window; both of its ends are the answer."""
        kept = decimate(list(range(SERIES_ROWS)), 10)
        assert kept[0] == 0
        assert kept[-1] == SERIES_ROWS - 1

    def test_the_kept_rows_are_evenly_spaced_and_ascending(self) -> None:
        kept = decimate(list(range(1000)), 11)
        gaps = {b - a for a, b in zip(kept, kept[1:], strict=False)}
        assert gaps == {99, 100}
        assert kept == sorted(kept)

    def test_a_limit_of_one_returns_the_most_recent_reading(self) -> None:
        assert decimate(list(range(SERIES_ROWS)), 1) == [SERIES_ROWS - 1]

    def test_it_never_returns_a_duplicate(self) -> None:
        kept = decimate(list(range(SERIES_ROWS)), 50)
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


class TestScanCap:
    def test_an_ordinary_window_passes(self) -> None:
        assert check_scan_cap(1_000) is None

    def test_a_window_exactly_at_the_cap_is_allowed(self) -> None:
        """The cap is the most we are willing to scan, not one less than that."""
        assert check_scan_cap(MAX_ROWS_SCANNED) is None

    def test_a_wider_window_is_refused(self) -> None:
        with pytest.raises(WindowTooLarge):
            check_scan_cap(MAX_ROWS_SCANNED + 1)

    def test_the_refusal_names_both_numbers(self) -> None:
        """The consumer has to be told what to narrow, and by roughly how much."""
        with pytest.raises(WindowTooLarge) as caught:
            check_scan_cap(MAX_ROWS_SCANNED + 1)
        assert str(MAX_ROWS_SCANNED + 1) in str(caught.value)
        assert str(MAX_ROWS_SCANNED) in str(caught.value)
        assert caught.value.rows == MAX_ROWS_SCANNED + 1
        assert caught.value.cap == MAX_ROWS_SCANNED

    def test_an_explicit_cap_overrides_the_default(self) -> None:
        with pytest.raises(WindowTooLarge):
            check_scan_cap(11, cap=10)

    def test_the_default_cap_is_read_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So a test can lower it without every caller having to thread it through,
        and so raising it in config takes effect without re-importing anything."""
        monkeypatch.setattr(limits, "MAX_ROWS_SCANNED", 10)
        with pytest.raises(WindowTooLarge):
            check_scan_cap(11)
