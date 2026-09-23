"""How much of a window this service will scan, and how much of it it will emit.

Two different axes, neither of which knows where the rows came from:

- The **fetch** is bounded by :data:`~garmin_health.config.MAX_ROWS_SCANNED`,
  checked with a count before anything is built. Over that, the window is
  refused with a 413.
- The **response** is bounded by ``limit``, which means *even decimation across
  the requested window*, keeping the first and last readings.

A continuous heart-rate series is roughly one row every two minutes -- ~720 a
day, ~263k a year -- which is the shape both bounds were sized against. Any
provider serving a window owes a consumer these same answers, which is why they
live here rather than beside one provider's queries.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from garmin_health.config import DEFAULT_LIMIT
from garmin_health.config import MAX_LIMIT
from garmin_health.config import MAX_ROWS_SCANNED

logger = logging.getLogger(__name__)


class WindowTooLarge(Exception):
    """The requested window would scan more rows than we are willing to. -> 413."""

    def __init__(self, rows: int, cap: int) -> None:
        super().__init__(
            f"The requested window covers {rows} rows, more than the {cap} this service will "
            f"scan in one request. Narrow start/end and try again."
        )
        self.rows = rows
        self.cap = cap


class InvalidLimit(Exception):
    """``limit`` was present but not a positive integer. -> 400."""


def resolve_limit(limit: int | None) -> int:
    """Turn the consumer's ``limit`` into the number of samples we will emit.

    ``None`` becomes ``DEFAULT_LIMIT`` so an unbounded request cannot exhaust
    memory, and anything over ``MAX_LIMIT`` is clamped rather than refused --
    ``limit`` is a resolution knob, and an over-large one is still answerable.
    Zero and negatives are refused, because clamping either up to one sample or
    out to everything would be inventing an intent.
    """
    if limit is None:
        return DEFAULT_LIMIT
    if limit <= 0:
        raise InvalidLimit(f"limit must be a positive integer, got {limit}")
    if limit > MAX_LIMIT:
        logger.info("Clamping requested limit %s to %s.", limit, MAX_LIMIT)
        return MAX_LIMIT
    return limit


def check_scan_cap(rows: int, cap: int | None = None) -> None:
    """Refuse a window that would scan more than ``cap`` rows.

    Given a **count** rather than the rows themselves: the entire point is to
    refuse before hundreds of thousands of objects are built, so callers pass the
    result of a count query.

    ``cap`` defaults to ``MAX_ROWS_SCANNED`` read at *call* time rather than
    bound at import, so lowering it in one place reaches every caller.
    """
    ceiling = MAX_ROWS_SCANNED if cap is None else cap
    if rows > ceiling:
        raise WindowTooLarge(rows, ceiling)


def decimate[T](rows: Sequence[T], limit: int | None) -> list[T]:
    """Keep ``limit`` evenly-spaced rows, preserving the first and the last.

    This is **selection, not aggregation**: every row returned is a real reading
    that was recorded at the instant it carries. A bucket mean would produce a
    ``Sample`` whose timestamp names a moment at which nothing was measured, and
    ``Sample(timestamp, value)`` asserts "the reading at this instant".

    A consumer asking for a week at ``limit=200`` asked for a week and said 200
    points is enough resolution. Most-recent-N would silently return the last 6.7
    hours and discard the ``start`` they explicitly passed; the requested window
    is the primary selector. The spec's own client agrees: unlike its sleep and
    workout merges, ``get_time_series_merged`` never re-applies ``limit``.

    The honest cost is that a short excursion between two kept samples disappears
    -- a two-minute spike is invisible at ``limit=200`` over a week. At
    ``MAX_LIMIT`` a full day is never decimated, so this only bites on multi-week
    requests, where narrowing the window or raising the limit is the answer.
    """
    n = len(rows)
    if limit is None or n <= limit:
        return list(rows)
    if limit == 1:
        return [rows[-1]]
    return [rows[round(i * (n - 1) / (limit - 1))] for i in range(limit)]
