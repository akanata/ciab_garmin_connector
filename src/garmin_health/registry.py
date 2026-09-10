"""``METRICS``: what ``/v1/metrics`` advertises and ``/v1/time-series`` serves.

One declarative block per metric. Every builder comes from the generic factory in
``garmin/sampling.py``, so an entry is data rather than code, and the descriptor is
derived from the spec class itself rather than restated -- a descriptor advertising
``bpm`` for a series that emits ``ms`` would be a silent unit error in a merged
cross-provider list.

> **Never add an interval-valued metric here.** ``TimeSeries.samples`` is declared
> as bare ``list[Sample]``, and the consumer's client registers a structure hook
> for ``Sample`` that resolves by MRO -- so an ``IntervalSample`` served on
> ``/v1/time-series`` arrives with ``end_timestamp`` silently discarded. Sleep
> stages reach consumers only through ``SleepSession.stages``, where the
> parametrized-generic path preserves all three fields. ``tests/test_registry.py``
> enforces this structurally.

Deliberately **not** served, so nobody re-litigates it: the eight
``sleep_score_*`` sub-scores (GarminDB imports only ``sleepScores.overall.value``),
``readiness_hrv_balance`` (mapping ``hrv.status``'s four-value ordinal to a 0-100
score is an invention that would be concatenated with Oura's real numbers),
``readiness_score`` (GarminDB does not import Training Readiness; Body Battery
measures energy reserve, not recovery), and every temperature metric (GarminDB
3.9.0 has no skin-temperature column anywhere).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from typing import cast

import attrs
from health_data_service import HRV_RMSSD
from health_data_service import HeartRate
from health_data_service import MetricKind
from health_data_service import MetricType
from health_data_service import ReadinessRestingHeartRate
from health_data_service import Sample
from health_data_service import SleepScore
from health_data_service import TimeSeries

from garmin_health.garmin import daily
from garmin_health.garmin import heart_rate as hr
from garmin_health.garmin.sampling import Builder
from garmin_health.garmin.sampling import Probe

SOURCE = "garmin"

_KEEP_DEFAULT = object()


@attrs.frozen
class MetricEntry:
    """Everything the serving layer needs to know about one metric."""

    descriptor: MetricType
    series_cls: type[TimeSeries]
    unit: str | None
    build: Builder
    provenance: str
    probe: Probe

    def series(self, samples: list[Sample[Any]]) -> TimeSeries:
        """Wrap built samples in this metric's spec class.

        Keyword arguments only: attrs moves overridden base fields to the end, so
        ``HeartRate.__init__`` is ``(source, metric_id=..., ..., samples=[])`` --
        positional construction produces garbage the moment the spec adds a field.

        The cast is load-bearing for mypy, not a shrug: ``TimeSeries`` declares
        ``metric_id``, ``display_name``, ``unit`` and ``samples`` as *required*,
        and it is only the concrete subclasses that default them. Every class this
        registry holds is such a subclass -- ``_entry`` proves it by constructing
        an exemplar from ``source`` alone -- but that fact is not expressible in
        ``type[TimeSeries]``.
        """
        factory = cast(Callable[..., TimeSeries], self.series_cls)
        return factory(source=SOURCE, unit=self.unit, samples=samples)


def _entry(
    series_cls: type[TimeSeries],
    *,
    build: Builder,
    probe: Probe,
    provenance: str,
    unit: Any = _KEEP_DEFAULT,
) -> MetricEntry:
    """Build an entry, taking metric_id and display_name from the spec class.

    Constructing the exemplar from ``source`` alone is also the check that
    ``series_cls`` really is a concrete metric class with every other field
    defaulted, rather than a bare ``TimeSeries``.
    """
    exemplar = cast(Callable[..., TimeSeries], series_cls)(source=SOURCE)
    resolved = exemplar.unit if unit is _KEEP_DEFAULT else unit
    return MetricEntry(
        descriptor=MetricType(
            metric_id=exemplar.metric_id,
            display_name=exemplar.display_name,
            kind=MetricKind.TIME_SERIES,
            unit=resolved,
        ),
        series_cls=series_cls,
        unit=resolved,
        build=build,
        provenance=provenance,
        probe=probe,
    )


METRICS: dict[str, MetricEntry] = {
    entry.descriptor.metric_id: entry
    for entry in (
        _entry(
            HeartRate,
            build=hr.build_heart_rate,
            probe=hr.has_heart_rate,
            provenance="garmin_monitoring.db:monitoring_hr.heart_rate",
        ),
        _entry(
            HRV_RMSSD,
            # The spec defaults unit to None, but the column is RMSSD in ms.
            unit="ms",
            build=hr.build_hrv_rmssd,
            probe=hr.has_hrv,
            provenance="garmin_monitoring.db:monitoring_hrv_value.hrv",
        ),
        _entry(
            SleepScore,
            build=daily.build_sleep_score,
            probe=daily.has_sleep_score,
            provenance="garmin.db:sleep.score",
        ),
        _entry(
            ReadinessRestingHeartRate,
            unit="bpm",
            build=daily.build_resting_heart_rate,
            probe=daily.has_resting_heart_rate,
            provenance="garmin.db:resting_hr.resting_heart_rate (falls back to daily_summary.rhr)",
        ),
    )
}
