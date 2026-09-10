"""One ``SleepSession`` per night, assembled from four GarminDB tables.

The governing rule is that nothing here fabricates a value. Where Garmin recorded
nothing, the field is ``None`` and, if the whole window is unknowable, the session
is skipped with a warning. A synthesized sleep window looks perfectly valid, merges
into ``get_sleep_sessions_merged`` alongside real ones, and silently poisons every
downstream aggregate; a gap in the list is at least detectable.

The second rule is self-consistency. ``sleep_events`` shares a clock with
``monitoring_hr`` and ``monitoring_hrv_value``, while ``sleep.start``/``sleep.end``
are rendered in the *importing container's* TZ -- so whenever events exist, the
window is derived from them and the session's sub-series are guaranteed to line up
with the window that contains them.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Any

import attrs
import fitfile.conversions
from garmindb.garmindb import Hrv
from garmindb.garmindb import MonitoringHrvStatus
from garmindb.garmindb import MonitoringRespirationRate
from garmindb.garmindb import Sleep
from garmindb.garmindb import SleepEvents
from health_data_service import BreathRateAvg
from health_data_service import Count
from health_data_service import Duration
from health_data_service import Efficiency
from health_data_service import HRVAvg
from health_data_service import HeartRateAvg
from health_data_service import HeartRateMin
from health_data_service import IntervalSample
from health_data_service import Score
from health_data_service import SleepSession
from health_data_service import SleepStage
from health_data_service import SleepStages
from sqlalchemy.orm import Session

from garmin_health.config import MAX_ROWS_SCANNED
from garmin_health.garmin.connection import GarminConnection
from garmin_health.garmin.heart_rate import heart_rate_stats
from garmin_health.garmin.heart_rate import session_heart_rate
from garmin_health.garmin.heart_rate import session_hrv
from garmin_health.garmin.heart_rate import window_hrv_average
from garmin_health.garmin.sampling import WindowTooLarge
from garmin_health.garmin.sampling import period_count
from garmin_health.garmin.sampling import period_rows
from garmin_health.garmin.vocabulary import stage_for_event
from garmin_health.timezones import TimeZonePolicy

logger = logging.getLogger(__name__)

SOURCE = "garmin"

# Garmin's calendarDate-to-bedtime semantics are not worth guessing at, so events
# are searched over a wide window around `day` and then clustered.
EVENT_SEARCH_WINDOW = dt.timedelta(hours=24)
# Two consecutive nights are ~16h apart; stages within a night are minutes apart.
CLUSTER_GAP = dt.timedelta(hours=3)
# Where a night sits relative to its own calendar date, used only to pick a
# cluster when sleep.start is null and there is nothing better to anchor on.
TYPICAL_SLEEP_MIDPOINT = dt.timedelta(hours=3)
# Sleep rows are keyed on local midnight but a night starts the evening before, so
# the row query is widened by a day at each end and the results filtered on the
# derived start.
DAY_MARGIN = dt.timedelta(days=1)


@attrs.frozen
class _Interval:
    """One cleaned stage interval, half-open, in aware UTC."""

    start: dt.datetime
    end: dt.datetime
    stage: SleepStage


def _minutes(value: dt.time | None) -> float | None:
    """A GarminDB ``Time`` duration column as minutes."""
    if value is None:
        return None
    seconds = fitfile.conversions.time_to_secs(value)
    return None if seconds is None else seconds / 60


def _duration(value: float | None) -> Duration | None:
    return None if value is None else Duration(value=float(value), source=SOURCE)


def _cluster_events(rows: Sequence[Any]) -> list[list[Any]]:
    """Split timestamp-ordered event rows into one cluster per night."""
    clusters: list[list[Any]] = []
    current: list[Any] = []
    for row in rows:
        if current and row[0] - current[-1][0] > CLUSTER_GAP:
            clusters.append(current)
            current = []
        current.append(row)
    if current:
        clusters.append(current)
    return clusters


def _select_cluster(
    clusters: Sequence[Sequence[Any]],
    *,
    start_local: dt.datetime | None,
    day: dt.datetime,
) -> Sequence[Any] | None:
    """Pick the cluster belonging to this night.

    ``sleep.start`` is the better anchor when present -- moved onto the home clock
    first, so both sides of the comparison are the same clock. Failing that, the
    cluster whose midpoint sits closest to the small hours of ``day``.
    """
    if not clusters:
        return None
    if start_local is not None:
        return min(clusters, key=lambda c: abs(c[0][0] - start_local))
    target = day + TYPICAL_SLEEP_MIDPOINT
    return min(clusters, key=lambda c: abs(c[0][0] + (c[-1][0] - c[0][0]) / 2 - target))


def _intervals(cluster: Sequence[Any], tz: TimeZonePolicy, *, fill_gaps: bool) -> list[_Interval]:
    """Clean one cluster into a monotone timeline of half-open UTC intervals.

    ``duration`` is ``NOT NULL DEFAULT time.min``, so a zero-length row means
    "never recorded" and is dropped rather than emitted as an instantaneous stage.

    **Overlaps are clamped to the successor's start.** On the FIT path ``duration``
    is literally ``next_ts - this_ts``, so an overlap only arises from a clock
    adjustment or a duplicate row, and a monotone timeline is what any consumer
    summing stage durations needs.

    **Gaps are left alone** unless asked otherwise: a gap means Garmin recorded
    nothing there, and UNKNOWN filler would be inventing data.
    """
    raw: list[tuple[dt.datetime, dt.datetime, SleepStage]] = []
    for timestamp, event, duration in cluster:
        seconds = fitfile.conversions.time_to_secs(duration) or 0
        if seconds <= 0:
            continue
        raw.append((timestamp, timestamp + dt.timedelta(seconds=seconds), stage_for_event(event)))

    cleaned: list[_Interval] = []
    for index, (start, end, stage) in enumerate(raw):
        if index + 1 < len(raw):
            end = min(end, raw[index + 1][0])
        if end <= start:
            continue
        cleaned.append(_Interval(start=tz.to_utc(start), end=tz.to_utc(end), stage=stage))

    if not fill_gaps:
        return cleaned

    filled: list[_Interval] = []
    for interval in cleaned:
        if filled and filled[-1].end < interval.start:
            filled.append(
                _Interval(start=filled[-1].end, end=interval.start, stage=SleepStage.UNKNOWN)
            )
        filled.append(interval)
    return filled


def _window_from_columns(row: Any, tz: TimeZonePolicy) -> tuple[dt.datetime, dt.datetime] | None:
    """The sleep.start / sleep.end fallbacks, in priority order.

    Both columns are on the *importing container's* clock, so both go through
    ``sleep_column_to_utc`` -- the one conversion in this codebase that applies the
    import skew.
    """
    start = tz.sleep_column_to_utc(row.start) if row.start is not None else None
    end = tz.sleep_column_to_utc(row.end) if row.end is not None else None
    if start is not None and end is not None:
        return start, end

    # A single endpoint plus a recorded span is still an honest window. total_sleep
    # and awake are both NOT NULL DEFAULT time.min, so zero means "not recorded".
    span_minutes = (_minutes(row.total_sleep) or 0) + (_minutes(row.awake) or 0)
    if span_minutes <= 0:
        return None
    span = dt.timedelta(minutes=span_minutes)
    if start is not None:
        return start, start + span
    if end is not None:
        return end - span, end
    return None


def _stage_totals(intervals: Sequence[_Interval]) -> dict[SleepStage, float]:
    totals: dict[SleepStage, float] = {}
    for interval in intervals:
        minutes = (interval.end - interval.start).total_seconds() / 60
        totals[interval.stage] = totals.get(interval.stage, 0.0) + minutes
    return totals


def _durations(
    row: Any, intervals: Sequence[_Interval]
) -> tuple[float | None, float | None, float | None, float | None, float | None]:
    """``(total, deep, light, rem, awake)`` in minutes, any of which may be None.

    All five columns are ``NOT NULL DEFAULT time.min``, so "no data" and "zero
    minutes" are indistinguishable on the wire. The policy, and its deliberate
    asymmetry: a **zero total** means the row was never populated, so the whole
    breakdown is recomputed from the stage intervals (the same arithmetic as
    ``SleepEvents.get_day_stats``) and is None if that yields nothing either. A
    **positive total** means the row was populated, so a zero stage is real data --
    a night with genuinely no REM -- and is emitted as 0.
    """
    total = _minutes(row.total_sleep) or 0.0
    if total > 0:
        return (
            total,
            _minutes(row.deep_sleep),
            _minutes(row.light_sleep),
            _minutes(row.rem_sleep),
            _minutes(row.awake),
        )

    if not intervals:
        return (None, None, None, None, None)

    totals = _stage_totals(intervals)
    deep = totals.get(SleepStage.DEEP, 0.0)
    light = totals.get(SleepStage.LIGHT, 0.0)
    rem = totals.get(SleepStage.REM, 0.0)
    awake = totals.get(SleepStage.AWAKE, 0.0)
    recomputed = deep + light + rem
    if recomputed <= 0:
        return (None, None, None, None, None)
    return (recomputed, deep, light, rem, awake)


def _efficiency(total_minutes: float | None, in_bed_minutes: float, day: str) -> Efficiency | None:
    """Sleep over time in bed, the textbook definition Oura also uses.

    A ratio above 100 is precisely the canary that ``total_sleep`` and the
    event-derived window disagree -- i.e. the timezone skew is still present -- so
    it is clamped *and* logged rather than swallowed.
    """
    if not total_minutes or in_bed_minutes <= 0:
        return None
    ratio = 100 * total_minutes / in_bed_minutes
    if ratio > 100:
        logger.warning(
            "Sleep efficiency for %s came out at %.1f%% (%.0f min sleep in a %.0f min window); "
            "clamping to 100. total_sleep and the session window disagree, which usually means "
            "an unresolved import timezone skew.",
            day,
            ratio,
            total_minutes,
            in_bed_minutes,
        )
        ratio = 100.0
    return Efficiency(value=ratio, source=SOURCE)


def _restless_periods(
    intervals: Sequence[_Interval], start: dt.datetime, end: dt.datetime
) -> Count:
    """Awake intervals strictly interior to the night.

    Off by default, and for a reason worth keeping: Oura's "restless periods" is a
    *movement*-derived count, not an awakening count. ``Count`` carries
    ``unit=None`` and ``display_name="Count"``, so a consumer cannot tell which one
    it received, and ``_fan_out`` merges every provider into one list. A
    wrong-semantics number under another vendor's metric id is exactly what
    corrupts a cross-provider merge.
    """
    interior = sum(
        1 for i in intervals if i.stage is SleepStage.AWAKE and i.start > start and i.end < end
    )
    return Count(value=interior, source=SOURCE)


def _average_hrv(
    session: Session,
    garmin_session: Session,
    tz: TimeZonePolicy,
    row: Any,
    start: dt.datetime,
    end: dt.datetime,
) -> HRVAvg | None:
    """Window average first, then the two day-keyed tables.

    ``monitoring_hrv_*`` is FIT-only and ``hrv`` is JSON-only -- they come from
    different ingest paths and neither is universally present. Both fallbacks are
    keyed on Garmin's day rather than our window, so they are logged when used.
    """
    windowed = window_hrv_average(session, tz, start, end)
    if windowed is not None:
        return HRVAvg(value=windowed, source=SOURCE)

    status = MonitoringHrvStatus.s_get_col_max(
        session,
        MonitoringHrvStatus.last_night_average,
        tz.to_naive_local(start),
        tz.to_naive_local(end),
    )
    if status is not None:
        logger.info(
            "No monitoring_hrv_value rows for %s; using monitoring_hrv_status.last_night_average, "
            "which is keyed on Garmin's day rather than this session's window.",
            row.day.date(),
        )
        return HRVAvg(value=float(status), source=SOURCE)

    daily = (
        garmin_session.query(Hrv.last_night_avg)
        .filter(Hrv.day == row.day)
        .filter(Hrv.last_night_avg.is_not(None))
        .first()
    )
    if daily is not None:
        logger.info(
            "No FIT HRV for %s; using the JSON hrv.last_night_avg column, which is an integer "
            "and is keyed on Garmin's day rather than this session's window.",
            row.day.date(),
        )
        return HRVAvg(value=float(daily[0]), source=SOURCE)
    return None


def _average_breath(
    session: Session, tz: TimeZonePolicy, row: Any, start: dt.datetime, end: dt.datetime
) -> BreathRateAvg | None:
    """``sleep.avg_rr`` first, then the monitoring window average.

    Deliberately the opposite preference to HRV, and that is fine *because*
    ``SleepSession`` has no respiration sub-series for it to disagree with. Nothing
    constrains it to our window, so matching what the Connect app shows wins.
    """
    if row.avg_rr is not None:
        return BreathRateAvg(value=float(row.avg_rr), source=SOURCE)
    average = MonitoringRespirationRate.s_get_col_avg(
        session,
        MonitoringRespirationRate.rr,
        tz.to_naive_local(start),
        tz.to_naive_local(end),
        ignore_le_zero=True,
    )
    if average is None:
        return None
    return BreathRateAvg(value=float(average), source=SOURCE)


def _build_one(
    row: Any,
    clusters: Sequence[Sequence[Any]],
    conn: GarminConnection,
    garmin_session: Session,
    monitoring_session: Session,
) -> SleepSession | None:
    tz = conn.tz
    day_label = row.day.date().isoformat()

    start_local = (
        tz.to_naive_local(tz.sleep_column_to_utc(row.start)) if row.start is not None else None
    )
    cluster = _select_cluster(clusters, start_local=start_local, day=row.day)
    intervals = _intervals(cluster, tz, fill_gaps=conn.settings.fill_stage_gaps) if cluster else []

    # Priority 1: derive the window from the events themselves, even when
    # start/end are non-null, because events share a clock with the sub-series.
    # "Events present" means at least one interval survived cleaning -- a cluster
    # of zero-length rows carries no duration information to derive a window from.
    if intervals:
        start, end = intervals[0].start, max(i.end for i in intervals)
    else:
        window = _window_from_columns(row, tz)
        if window is None:
            logger.warning(
                "Skipping the sleep session for %s: no events, and sleep.start/end give no "
                "usable window. Serving a synthesized one would look valid and silently corrupt "
                "any consumer that aggregates it.",
                day_label,
            )
            return None
        start, end = window

    if end <= start:
        logger.warning(
            "Skipping the sleep session for %s: the derived window ends at or before it starts "
            "(%s to %s).",
            day_label,
            start,
            end,
        )
        return None

    total, deep, light, rem, awake = _durations(row, intervals)
    in_bed_minutes = (end - start).total_seconds() / 60
    # get_stats already passes ignore_le_zero=True, so a 0 bpm dropout cannot
    # become the minimum. Both are tested for `is not None`, never truthiness.
    average_hr, lowest_hr = heart_rate_stats(monitoring_session, tz, start, end)

    return SleepSession(
        start=start,
        end=end,
        id=f"garmin:sleep:{day_label}",
        source=SOURCE,
        # Reachable ONLY here. TimeSeries.samples is declared as bare list[Sample]
        # and the consumer's structure hook resolves by MRO, so an interval-valued
        # metric served on /v1/time-series silently loses end_timestamp.
        stages=(
            SleepStages(
                source=SOURCE,
                samples=[
                    IntervalSample(timestamp=i.start, value=i.stage, end_timestamp=i.end)
                    for i in intervals
                ],
            )
            if intervals
            else None
        ),
        heart_rate=session_heart_rate(monitoring_session, tz, start, end),
        hrv=session_hrv(monitoring_session, tz, start, end),
        total_duration=_duration(total),
        deep_sleep_duration=_duration(deep),
        light_sleep_duration=_duration(light),
        rem_sleep_duration=_duration(rem),
        awake_time=_duration(awake),
        # Garmin's detected sleep window IS the in-bed span, so this is shorter
        # than true bed time by the pre-sleep reading period.
        time_in_bed=_duration(in_bed_minutes),
        # Lights-out to onset, which GarminDB simply does not have: sleep.start is
        # the detected onset and the first event is already a sleep stage, so every
        # derivation is structurally zero or noise. Garmin Connect exposes
        # sleepLatencySeconds, but GarminDB 3.9.0 reads ten keys from
        # dailySleepDTO and that is not one of them.
        latency=None,
        average_heart_rate=(
            HeartRateAvg(value=average_hr, source=SOURCE) if average_hr is not None else None
        ),
        lowest_heart_rate=(
            HeartRateMin(value=lowest_hr, source=SOURCE) if lowest_hr is not None else None
        ),
        average_hrv=_average_hrv(monitoring_session, garmin_session, tz, row, start, end),
        average_breath=_average_breath(monitoring_session, tz, row, start, end),
        efficiency=_efficiency(total, in_bed_minutes, day_label),
        restless_periods=(
            _restless_periods(intervals, start, end)
            if conn.settings.derive_restless_periods
            else None
        ),
        sleep_score=(
            Score(value=float(row.score), source=SOURCE) if row.score is not None else None
        ),
    )


def build_sleep_sessions(
    conn: GarminConnection,
    start_utc: dt.datetime | None,
    end_utc: dt.datetime | None,
    limit: int | None,
) -> list[SleepSession]:
    """Every sleep session starting within ``[start, end)``, newest first.

    Sorted descending by ``start`` to match ``get_sleep_sessions_merged``, which
    unlike the time-series merge *does* re-apply ``limit`` -- so newest-first
    truncation is what a consumer expects here.
    """
    tz = conn.tz
    lo = tz.to_naive_local(start_utc) if start_utc is not None else None
    hi = tz.to_naive_local(end_utc) if end_utc is not None else None
    # Sleep rows are keyed on local midnight but a night begins the previous
    # evening, so the row scan is widened and the results filtered on the derived
    # start below.
    lo_day = lo - DAY_MARGIN if lo is not None else None
    hi_day = hi + DAY_MARGIN if hi is not None else None

    def query(g: Session, m: Session) -> list[SleepSession]:
        rows = Sleep.s_get_for_period(g, lo_day, hi_day)
        if not rows:
            return []

        event_lo = lo_day - EVENT_SEARCH_WINDOW if lo_day is not None else None
        event_hi = hi_day + EVENT_SEARCH_WINDOW if hi_day is not None else None
        events_scanned = period_count(g, SleepEvents, start=event_lo, end=event_hi)
        if events_scanned > MAX_ROWS_SCANNED:
            raise WindowTooLarge(events_scanned, MAX_ROWS_SCANNED)
        clusters = _cluster_events(
            period_rows(
                g,
                SleepEvents,
                SleepEvents.timestamp,
                SleepEvents.event,
                SleepEvents.duration,
                start=event_lo,
                end=event_hi,
            )
        )

        built = []
        for row in rows:
            session = _build_one(row, clusters, conn, g, m)
            if session is not None:
                built.append(session)
        return built

    sessions = conn.read(query)
    sessions = [
        s
        for s in sessions
        if (start_utc is None or s.start >= start_utc) and (end_utc is None or s.start < end_utc)
    ]
    sessions.sort(key=lambda s: s.start, reverse=True)
    return sessions if limit is None else sessions[:limit]
