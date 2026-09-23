"""The four metrics this provider contributes, bound to one corpus.

Deliberately **not** served, so nobody re-litigates it: the eight
``sleep_score_*`` sub-scores (GarminDB imports only ``sleepScores.overall.value``),
``readiness_hrv_balance`` (mapping ``hrv.status``'s four-value ordinal to a 0-100
score is an invention that would be concatenated with Oura's real numbers),
``readiness_score`` (GarminDB does not import Training Readiness; Body Battery
measures energy reserve, not recovery), and every temperature metric (GarminDB
3.9.0 has no skin-temperature column anywhere).

Each entry is bound to the connection it was built for, so the generic
``MetricEntry`` never names a ``GarminConnection``. The builders and probes
themselves still take one, because that is how ``sampling.py`` composes them;
:func:`functools.partial` is what closes that gap.
"""

from __future__ import annotations

from functools import partial

from health_data_service import HRV_RMSSD
from health_data_service import HeartRate
from health_data_service import ReadinessRestingHeartRate
from health_data_service import SleepScore

from garmin_health.providers.garmindb import daily
from garmin_health.providers.garmindb import heart_rate as hr
from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.registry import MetricEntry
from garmin_health.registry import metric_entry


def metrics_for(conn: GarminConnection) -> dict[str, MetricEntry]:
    """The catalog this corpus can serve, keyed by metric id."""
    entries = (
        metric_entry(
            HeartRate,
            build=partial(hr.build_heart_rate, conn),
            probe=partial(hr.has_heart_rate, conn),
            provenance="garmin_monitoring.db:monitoring_hr.heart_rate",
        ),
        metric_entry(
            HRV_RMSSD,
            # The spec defaults unit to None, but the column is RMSSD in ms.
            unit="ms",
            build=partial(hr.build_hrv_rmssd, conn),
            probe=partial(hr.has_hrv, conn),
            provenance="garmin_monitoring.db:monitoring_hrv_value.hrv",
        ),
        metric_entry(
            SleepScore,
            build=partial(daily.build_sleep_score, conn),
            probe=partial(daily.has_sleep_score, conn),
            provenance="garmin.db:sleep.score",
        ),
        metric_entry(
            ReadinessRestingHeartRate,
            unit="bpm",
            build=partial(daily.build_resting_heart_rate, conn),
            probe=partial(daily.has_resting_heart_rate, conn),
            provenance="garmin.db:resting_hr.resting_heart_rate (falls back to daily_summary.rhr)",
        ),
    )
    return {entry.descriptor.metric_id: entry for entry in entries}
