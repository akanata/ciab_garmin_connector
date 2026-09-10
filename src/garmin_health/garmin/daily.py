"""Day-keyed series: one sample per Garmin calendar date.

These tables are keyed on ``day``, a naive **local midnight**, and go through the
same ``to_utc`` as everything else -- so local midnight in Denver becomes
``T06:00:00+00:00`` in summer rather than ``00:00Z``. Emitting ``00:00Z`` would be
a second, inconsistent convention that places the sample on the wrong local day.

The cost is that a consumer bucketing by *UTC* date is off by one in western
timezones. That is inherent rather than a choice: the spec has no date-valued
sample type, so a day has to be expressed as an instant somehow.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from garmindb.garmindb import DailySummary
from garmindb.garmindb import RestingHeartRate
from garmindb.garmindb import Sleep
from health_data_service import Sample
from sqlalchemy.orm import Session

from garmin_health.garmin.connection import GarminConnection
from garmin_health.garmin.sampling import column_series
from garmin_health.garmin.sampling import decimate
from garmin_health.garmin.sampling import has_rows
from garmin_health.garmin.sampling import period_rows

logger = logging.getLogger(__name__)

# sleep.score is nullable -- a night Garmin did not score is not a night that
# scored zero -- so the null filter is pushed into SQL.
build_sleep_score = column_series(Sleep, Sleep.score, db="garmin", skip_none=True)
has_sleep_score = has_rows(Sleep, Sleep.score, db="garmin")

_has_resting_hr_table = has_rows(RestingHeartRate, RestingHeartRate.resting_heart_rate, db="garmin")
_has_daily_summary_rhr = has_rows(DailySummary, DailySummary.rhr, db="garmin")


def build_resting_heart_rate(
    conn: GarminConnection,
    start_utc: dt.datetime | None,
    end_utc: dt.datetime | None,
    limit: int | None,
) -> list[Sample[float]]:
    """Daily resting heart rate, preferring ``resting_hr`` over ``daily_summary``.

    The two tables are populated by different downloads, so a corpus can easily
    have one and not the other. The fallback applies to the whole window rather
    than per day: merging them would mean choosing between two providers' numbers
    for the same date on every row, and neither is more authoritative.
    """
    tz = conn.tz
    lo = tz.to_naive_local(start_utc) if start_utc is not None else None
    hi = tz.to_naive_local(end_utc) if end_utc is not None else None

    def query(g: Session, _: Session) -> tuple[list[Any], bool]:
        rows = period_rows(
            g,
            RestingHeartRate,
            RestingHeartRate.time_col,
            RestingHeartRate.resting_heart_rate,
            start=lo,
            end=hi,
            not_none_col=RestingHeartRate.resting_heart_rate,
        )
        if rows:
            return rows, False
        return (
            period_rows(
                g,
                DailySummary,
                DailySummary.time_col,
                DailySummary.rhr,
                start=lo,
                end=hi,
                not_none_col=DailySummary.rhr,
            ),
            True,
        )

    rows, used_fallback = conn.read(query)
    if used_fallback and rows:
        logger.info(
            "resting_hr holds nothing for this window; falling back to daily_summary.rhr "
            "(%s rows).",
            len(rows),
        )
    return [
        Sample(timestamp=tz.to_utc(day), value=float(value)) for day, value in decimate(rows, limit)
    ]


def has_resting_heart_rate(conn: GarminConnection) -> bool:
    """True if either source table holds a value, matching the builder's fallback."""
    return _has_resting_hr_table(conn) or _has_daily_summary_rhr(conn)
