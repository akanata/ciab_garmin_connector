"""``MetricEntry``: the record every provider's catalog is made of.

One entry per metric, and an entry is *data* rather than code: the descriptor is
derived from the spec class itself rather than restated, because a descriptor
advertising ``bpm`` for a series that emits ``ms`` would be a silent unit error
in a merged cross-provider list.

> **Never add an interval-valued metric to any provider's catalog.**
> ``TimeSeries.samples`` is declared as bare ``list[Sample]``, and the consumer's
> client registers a structure hook for ``Sample`` that resolves by MRO -- so an
> ``IntervalSample`` served on ``/v1/time-series`` arrives with ``end_timestamp``
> silently discarded. Sleep stages reach consumers only through
> ``SleepSession.stages``, where the parametrized-generic path preserves all
> three fields. ``tests/test_registry.py`` enforces this structurally.

:data:`Builder` and :data:`Probe` are **bound**: they take no connection, no
session, no client. A provider closes over whatever it reads from when it builds
its entries, which is what keeps this record free of any provider's types. The
entries themselves live with their provider -- GarminDB's four, and the reasons
several spec metrics are deliberately not among them, are in
``providers/garmindb/registry.py``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any
from typing import cast

import attrs
from health_data_service import MetricKind
from health_data_service import MetricType
from health_data_service import Sample
from health_data_service import TimeSeries

from garmin_health.ports import SOURCE

_KEEP_DEFAULT = object()

Builder = Callable[["dt.datetime | None", "dt.datetime | None", "int | None"], "list[Sample[Any]]"]
Probe = Callable[[], bool]


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
        and it is only the concrete subclasses that default them. Every class an
        entry holds is such a subclass -- :func:`metric_entry` proves it by
        constructing an exemplar from ``source`` alone -- but that fact is not
        expressible in ``type[TimeSeries]``.
        """
        factory = cast(Callable[..., TimeSeries], self.series_cls)
        return factory(source=SOURCE, unit=self.unit, samples=samples)


def metric_entry(
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

    Leaving ``unit`` unset keeps the spec class's own. Passing it -- even as
    ``None`` -- overrides, which is how a column with a real unit that the spec
    defaults to ``None`` gets advertised correctly.
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
