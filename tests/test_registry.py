"""``MetricEntry``: what every provider's catalog entry has to be.

The entries themselves are a provider's business -- GarminDB's four live in
``tests/providers/garmindb/test_registry.py``. What is here is the record they
all have to fit, and the one structural trap it exists to prevent.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import attrs
import pytest
from health_data_service import HRV_RMSSD
from health_data_service import HeartRate
from health_data_service import MetricKind
from health_data_service import Sample
from health_data_service import SleepScore
from health_data_service import SleepStages
from health_data_service import TimeSeries

from garmin_health.ports import SOURCE
from garmin_health.registry import MetricEntry
from garmin_health.registry import metric_entry


def _no_samples(
    start: dt.datetime | None, end: dt.datetime | None, limit: int | None
) -> list[Sample[Any]]:
    return []


def _entry(series_cls: type[TimeSeries] = HeartRate, **kwargs: Any) -> MetricEntry:
    kwargs.setdefault("build", _no_samples)
    kwargs.setdefault("probe", lambda: True)
    kwargs.setdefault("provenance", "test:column")
    return metric_entry(series_cls, **kwargs)


class TestMetricEntry:
    def test_the_descriptor_is_taken_from_the_spec_class(self) -> None:
        """Restating metric_id or display_name here would let the catalog drift
        from what the series actually says it is."""
        entry = _entry(HeartRate)
        assert entry.descriptor.metric_id == "heart_rate"
        assert entry.descriptor.display_name == HeartRate(source=SOURCE).display_name

    def test_every_metric_is_advertised_as_a_time_series(self) -> None:
        assert _entry(SleepScore).descriptor.kind is MetricKind.TIME_SERIES

    def test_the_unit_defaults_to_the_spec_classes_own(self) -> None:
        assert _entry(HeartRate).descriptor.unit == "bpm"

    def test_a_unit_can_be_overridden_where_the_column_has_a_real_one(self) -> None:
        """The spec defaults HRV's unit to None, but the column is RMSSD in ms."""
        entry = _entry(HRV_RMSSD, unit="ms")
        assert entry.descriptor.unit == "ms"
        assert entry.series([]).unit == "ms"

    def test_an_explicit_none_unit_is_kept(self) -> None:
        """Distinct from 'not given': a 0-100 score genuinely has no unit."""
        assert _entry(SleepScore, unit=None).descriptor.unit is None

    def test_the_descriptor_matches_the_series_it_will_build(self) -> None:
        """A descriptor advertising bpm for a series that emits ms would be a
        silent unit error in a merged cross-provider list."""
        for series_cls in (HeartRate, SleepScore):
            entry = _entry(series_cls)
            series = entry.series([])
            assert entry.descriptor.metric_id == series.metric_id
            assert entry.descriptor.display_name == series.display_name
            assert entry.descriptor.unit == series.unit

    def test_a_built_series_carries_the_source_and_the_samples(self) -> None:
        samples = [Sample(timestamp=dt.datetime(2026, 9, 15, tzinfo=dt.UTC), value=60.0)]
        series = _entry(HeartRate).series(samples)
        assert isinstance(series, HeartRate)
        assert series.source == SOURCE
        assert series.samples == samples

    def test_an_entry_is_frozen_data(self) -> None:
        entry = _entry()
        assert isinstance(entry, MetricEntry)
        with pytest.raises(attrs.exceptions.FrozenInstanceError):
            entry.provenance = "somewhere else"  # type: ignore[misc]

    def test_a_builder_takes_only_a_window_and_a_limit(self) -> None:
        """Bound, with no connection argument: a provider closes over whatever it
        reads from, so this record stays free of any provider's types."""
        seen: list[tuple[Any, Any, Any]] = []

        def build(
            start: dt.datetime | None, end: dt.datetime | None, limit: int | None
        ) -> list[Sample[Any]]:
            seen.append((start, end, limit))
            return []

        _entry(build=build).build(None, None, 5)
        assert seen == [(None, None, 5)]

    def test_a_probe_takes_nothing(self) -> None:
        assert _entry(probe=lambda: False).probe() is False


class TestNoIntervalSamples:
    """TimeSeries.samples is declared as bare list[Sample], and the consumer's
    client registers a structure hook for Sample that resolves by MRO -- so an
    IntervalSample served on /v1/time-series comes back with end_timestamp
    silently discarded. Sleep stages must reach consumers only through
    SleepSession.stages.
    """

    @pytest.mark.parametrize("series_cls", [HeartRate, HRV_RMSSD, SleepScore])
    def test_a_servable_class_never_yields_interval_samples(
        self, series_cls: type[TimeSeries]
    ) -> None:
        annotation = attrs.fields(series_cls).samples.type
        assert "IntervalSample" not in str(annotation)

    def test_the_structural_guard_would_actually_catch_the_bad_case(self) -> None:
        """A guard that cannot fail is not a guard. SleepStages is the real class
        this protects against, so it must trip the same check."""
        assert "IntervalSample" in str(attrs.fields(SleepStages).samples.type)
