"""Heart rate and HRV: the two catalog series, and the sub-series of a night.

The catalog builders are ``column_series`` applied to the two monitoring tables.
The session-scoped functions here take an already-open monitoring session, because
``sleep.py`` builds a whole night's worth of series and scalars inside a single
paired read rather than reopening a session per field.
"""

from __future__ import annotations

import datetime as dt
import logging

from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import MonitoringHrvValue
from health_data_service import HRV_RMSSD
from health_data_service import HeartRate
from health_data_service import Sample
from sqlalchemy.orm import Session

from garmin_health.config import MAX_SESSION_SUBSERIES
from garmin_health.limits import decimate
from garmin_health.providers.garmindb.sampling import column_series
from garmin_health.providers.garmindb.sampling import has_rows
from garmin_health.providers.garmindb.sampling import period_rows
from garmin_health.providers.garmindb.timezones import TimeZonePolicy

logger = logging.getLogger(__name__)

SOURCE = "garmin"

# monitoring_hr.heart_rate is an Integer column; the spec carries Sample[float].
build_heart_rate = column_series(
    MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring"
)
has_heart_rate = has_rows(MonitoringHeartRate, MonitoringHeartRate.heart_rate, db="monitoring")

build_hrv_rmssd = column_series(MonitoringHrvValue, MonitoringHrvValue.hrv, db="monitoring")
has_hrv = has_rows(MonitoringHrvValue, MonitoringHrvValue.hrv, db="monitoring")


def _window_samples(
    session: Session,
    table: object,
    column: object,
    tz: TimeZonePolicy,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
) -> list[Sample[float]]:
    rows = period_rows(
        session,
        table,
        table.time_col,  # type: ignore[attr-defined]
        column,
        start=tz.to_naive_local(start_utc),
        end=tz.to_naive_local(end_utc),
    )
    return [
        Sample(timestamp=tz.to_utc(timestamp), value=float(value))
        for timestamp, value in decimate(rows, MAX_SESSION_SUBSERIES)
    ]


def session_heart_rate(
    session: Session, tz: TimeZonePolicy, start_utc: dt.datetime, end_utc: dt.datetime
) -> HeartRate | None:
    """The night's heart-rate trace, or None if the watch recorded none.

    None rather than an empty series on purpose: "no sensor data" and "a sensor
    that recorded nothing" are different claims, and only one of them is true here.
    """
    samples = _window_samples(
        session, MonitoringHeartRate, MonitoringHeartRate.heart_rate, tz, start_utc, end_utc
    )
    if not samples:
        return None
    return HeartRate(source=SOURCE, samples=samples)


def session_hrv(
    session: Session, tz: TimeZonePolicy, start_utc: dt.datetime, end_utc: dt.datetime
) -> HRV_RMSSD | None:
    """The night's HRV trace. ``unit`` is overridden: the column is RMSSD in ms."""
    samples = _window_samples(
        session, MonitoringHrvValue, MonitoringHrvValue.hrv, tz, start_utc, end_utc
    )
    if not samples:
        return None
    return HRV_RMSSD(source=SOURCE, unit="ms", samples=samples)


def heart_rate_stats(
    session: Session, tz: TimeZonePolicy, start_utc: dt.datetime, end_utc: dt.datetime
) -> tuple[float | None, float | None]:
    """``(average, lowest)`` over the window, or ``(None, None)``.

    ``MonitoringHeartRate.get_stats`` already passes ``ignore_le_zero=True``, which
    is what keeps a 0 bpm dropout out of the minimum. Callers must test
    ``is not None`` rather than truthiness.
    """
    stats = MonitoringHeartRate.get_stats(
        session, tz.to_naive_local(start_utc), tz.to_naive_local(end_utc)
    )
    average = stats.get("hr_avg")
    lowest = stats.get("hr_min")
    return (
        float(average) if average is not None else None,
        float(lowest) if lowest is not None else None,
    )


def window_hrv_average(
    session: Session, tz: TimeZonePolicy, start_utc: dt.datetime, end_utc: dt.datetime
) -> float | None:
    """Mean ``monitoring_hrv_value.hrv`` over exactly ``[start, end)``.

    Deliberately not ``Hrv.last_night_avg``. This is computed over the identical
    window as the ``hrv`` sub-series and ``average_heart_rate``, so a consumer
    averaging ``session.hrv.samples`` gets the same number back; ``hrv.day`` is
    Garmin's own day-keying, which silently attaches the *wrong night's* HRV to any
    session whose calendar attribution is off by one; and ``Hrv.last_night_avg`` is
    an Integer column, losing sub-ms precision.
    """
    average = MonitoringHrvValue.s_get_col_avg(
        session,
        MonitoringHrvValue.hrv,
        tz.to_naive_local(start_utc),
        tz.to_naive_local(end_utc),
    )
    return float(average) if average is not None else None
