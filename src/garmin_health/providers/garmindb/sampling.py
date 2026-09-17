"""Window queries, decimation, and the generic builder every metric is made of.

``monitoring_hr`` is roughly one row every two minutes -- ~720 a day, ~263k a
year -- so both the fetch and the response need bounding, on two different axes:

- The **fetch** is bounded by ``MAX_ROWS_SCANNED``, checked with a count query
  before anything is read. Over that, the window is refused with a 413.
- The **response** is bounded by ``limit``, which means *even decimation across
  the requested window*, keeping the first and last readings.

Selecting two columns rather than the mapped entity returns lightweight ``Row``
tuples instead of constructing hundreds of thousands of ORM instances -- roughly
the difference between 50 MB and 500 MB on a one-year request.

Note that the plan's ``selectable=(time_col, column)`` does **not** work:
``DbObject._s_query`` passes its ``selectable`` to ``session.query()`` as a single
entity, and SQLAlchemy 2.0 rejects a tuple there. :func:`period_rows` builds the
same query from ``DbObject``'s public ``during``/``after``/``before`` expressions
instead, so there is still no raw SQL anywhere.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from collections.abc import Sequence
from typing import Any
from typing import Literal

from health_data_service import Sample
from sqlalchemy import func
from sqlalchemy.orm import Session

from garmin_health.config import DEFAULT_LIMIT
from garmin_health.config import MAX_LIMIT
from garmin_health.config import MAX_ROWS_SCANNED
from garmin_health.providers.garmindb.connection import GarminConnection

logger = logging.getLogger(__name__)

Database = Literal["garmin", "monitoring"]
Builder = Callable[
    [GarminConnection, "dt.datetime | None", "dt.datetime | None", "int | None"],
    "list[Sample[Any]]",
]
Probe = Callable[[GarminConnection], bool]


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


def period_rows(
    session: Session,
    table: Any,
    *columns: Any,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    not_none_col: Any = None,
) -> list[Any]:
    """Half-open ``[start, end)`` on ``table.time_col``, ascending.

    Both bounds must already be **naive** local: SQLAlchemy's SQLite ``DATETIME``
    bind processor formats the fields and discards ``tzinfo``, so an aware bound
    is compared as the wrong wall clock and silently selects a wrong subset.
    ``None`` means unbounded on that side.
    """
    query = session.query(*columns).order_by(table.time_col)
    if start is not None:
        query = query.filter(table.after(start))
    if end is not None:
        query = query.filter(table.before(end))
    if not_none_col is not None:
        query = query.filter(not_none_col.is_not(None))
    return list(query.all())


def period_count(
    session: Session,
    table: Any,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    not_none_col: Any = None,
) -> int:
    """How many rows ``period_rows`` would return, without building any of them."""
    query = session.query(func.count(table.time_col))
    if start is not None:
        query = query.filter(table.after(start))
    if end is not None:
        query = query.filter(table.before(end))
    if not_none_col is not None:
        query = query.filter(not_none_col.is_not(None))
    count: int = query.scalar() or 0
    return count


def _bounds(
    conn: GarminConnection, start_utc: dt.datetime | None, end_utc: dt.datetime | None
) -> tuple[dt.datetime | None, dt.datetime | None]:
    tz = conn.tz
    return (
        tz.to_naive_local(start_utc) if start_utc is not None else None,
        tz.to_naive_local(end_utc) if end_utc is not None else None,
    )


def column_series(
    table: Any,
    column: Any,
    *,
    db: Database,
    cast: Callable[[Any], Any] = float,
    skip_none: bool = False,
) -> Builder:
    """A :data:`Builder` reading one column of one table over the window.

    ``skip_none`` pushes the null filter into SQL rather than dropping rows
    afterwards, which matters for more than tidiness: filtering after decimation
    would make ``limit=N`` return fewer than N samples for no visible reason.
    Set it for every nullable column.
    """

    def build(
        conn: GarminConnection,
        start_utc: dt.datetime | None,
        end_utc: dt.datetime | None,
        limit: int | None,
    ) -> list[Sample[Any]]:
        lo, hi = _bounds(conn, start_utc, end_utc)
        not_none_col = column if skip_none else None

        def query(g: Session, m: Session) -> list[Any]:
            session = g if db == "garmin" else m
            count = period_count(session, table, start=lo, end=hi, not_none_col=not_none_col)
            if count > MAX_ROWS_SCANNED:
                raise WindowTooLarge(count, MAX_ROWS_SCANNED)
            return period_rows(
                session,
                table,
                table.time_col,
                column,
                start=lo,
                end=hi,
                not_none_col=not_none_col,
            )

        rows = conn.read(query)
        tz = conn.tz
        return [
            Sample(timestamp=tz.to_utc(timestamp), value=cast(value))
            for timestamp, value in decimate(rows, limit)
        ]

    return build


def has_rows(table: Any, column: Any, *, db: Database) -> Probe:
    """A :data:`Probe` reporting whether ``column`` holds any non-null value.

    ``/v1/metrics`` filters on this because ``list_metrics_merged`` is what a
    consumer uses to decide what to request -- advertising an empty metric costs
    it a wasted round trip. A table of nothing but nulls counts as empty.
    """

    def probe(conn: GarminConnection) -> bool:
        def query(g: Session, m: Session) -> bool:
            session = g if db == "garmin" else m
            return session.query(table.time_col).filter(column.is_not(None)).first() is not None

        return conn.read(query)

    return probe
