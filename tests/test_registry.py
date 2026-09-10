"""The metric catalog: declarative data, and the invariants that keep it honest."""

from __future__ import annotations

from pathlib import Path

import attrs
import pytest
from health_data_service import IntervalSample
from health_data_service import MetricKind
from health_data_service import SleepStages
from health_data_service import TimeSeries

from garmin_health.config import Settings
from garmin_health.garmin.connection import GarminConnection
from garmin_health.registry import METRICS
from garmin_health.registry import MetricEntry
from tests.fixtures import HOME_TZ_NAME
from tests.fixtures import build_fixture


@pytest.fixture
def corpus_settings(tmp_path: Path) -> Settings:
    return Settings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


def test_every_key_is_its_own_metric_id() -> None:
    for key, entry in METRICS.items():
        assert key == entry.descriptor.metric_id


def test_metric_ids_are_unique() -> None:
    ids = [e.descriptor.metric_id for e in METRICS.values()]
    assert len(set(ids)) == len(ids)


def test_this_iteration_serves_the_four_planned_metrics() -> None:
    assert set(METRICS) == {
        "heart_rate",
        "hrv_rmssd",
        "sleep_score",
        "readiness_resting_heart_rate",
    }


def test_every_descriptor_matches_the_series_it_will_build() -> None:
    """A descriptor advertising bpm for a series that emits ms would be a silent
    unit error in a merged cross-provider list."""
    for entry in METRICS.values():
        series = entry.series([])
        assert entry.descriptor.metric_id == series.metric_id
        assert entry.descriptor.display_name == series.display_name
        assert entry.descriptor.unit == series.unit


def test_every_metric_is_a_time_series() -> None:
    assert all(e.descriptor.kind is MetricKind.TIME_SERIES for e in METRICS.values())


@pytest.mark.parametrize(
    ("metric_id", "unit"),
    [
        ("heart_rate", "bpm"),
        # The spec defaults both of these to None; the columns have real units.
        ("hrv_rmssd", "ms"),
        ("readiness_resting_heart_rate", "bpm"),
        # A 0-100 score genuinely has no unit.
        ("sleep_score", None),
    ],
)
def test_the_units_we_override(metric_id: str, unit: str | None) -> None:
    assert METRICS[metric_id].descriptor.unit == unit


def test_every_entry_names_the_column_it_reads() -> None:
    """Provenance is what makes a wrong number traceable to a table."""
    for entry in METRICS.values():
        assert ".db:" in entry.provenance


def test_no_entry_can_ever_yield_an_interval_sample() -> None:
    """The hazard this guards, in full: TimeSeries.samples is declared as bare
    list[Sample], and the consumer's client registers a structure hook for Sample
    that resolves by MRO -- so an IntervalSample served on /v1/time-series comes
    back with end_timestamp silently discarded. Sleep stages must reach consumers
    only through SleepSession.stages.

    Checked structurally rather than by sampling data, so an entry added against an
    empty table cannot slip through.
    """
    for entry in METRICS.values():
        annotation = attrs.fields(entry.series_cls).samples.type
        assert "IntervalSample" not in str(annotation), entry.descriptor.metric_id


def test_the_structural_guard_would_actually_catch_the_bad_case() -> None:
    """A guard that cannot fail is not a guard. SleepStages is the real class this
    is protecting against, so it must trip the same check."""
    assert "IntervalSample" in str(attrs.fields(SleepStages).samples.type)


def test_built_samples_are_never_interval_samples(corpus_settings: Settings) -> None:
    build_fixture(
        corpus_settings.health_data_dir,
        nights=3,
        heart_rate=True,
        hrv=True,
        sleep_score=82,
        resting_hr=True,
    )
    with GarminConnection(corpus_settings) as conn:
        for entry in METRICS.values():
            samples = entry.build(conn, None, None, None)
            assert samples, entry.descriptor.metric_id
            assert not any(isinstance(s, IntervalSample) for s in samples)


def test_every_entry_builds_a_series_of_its_own_class(corpus_settings: Settings) -> None:
    build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
    with GarminConnection(corpus_settings) as conn:
        for entry in METRICS.values():
            series = entry.series(entry.build(conn, None, None, None))
            assert isinstance(series, entry.series_cls)
            assert isinstance(series, TimeSeries)
            assert series.source == "garmin"


def test_probes_report_an_empty_corpus_as_having_nothing(
    corpus_settings: Settings,
) -> None:
    """/v1/metrics filters on the probe because list_metrics_merged is what a
    consumer uses to decide what to request."""
    build_fixture(corpus_settings.health_data_dir, nights=0)
    with GarminConnection(corpus_settings) as conn:
        assert not any(entry.probe(conn) for entry in METRICS.values())


def test_probes_report_exactly_what_the_corpus_holds(corpus_settings: Settings) -> None:
    build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
    with GarminConnection(corpus_settings) as conn:
        advertised = {k for k, e in METRICS.items() if e.probe(conn)}
    assert advertised == {"heart_rate"}


def test_an_entry_is_frozen_data(corpus_settings: Settings) -> None:
    entry = METRICS["heart_rate"]
    assert isinstance(entry, MetricEntry)
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        entry.provenance = "somewhere else"  # type: ignore[misc]
