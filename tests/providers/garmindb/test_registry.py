"""The four GarminDB metrics: what they advertise and what they read.

The shape of a ``MetricEntry`` is pinned generically in ``tests/test_registry.py``;
these are the entries this provider actually contributes, bound to a corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from health_data_service import IntervalSample
from health_data_service import TimeSeries

from garmin_health.config import Settings
from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.registry import metrics_for
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import build_fixture

EXPECTED = {"heart_rate", "hrv_rmssd", "sleep_score", "readiness_resting_heart_rate"}


@pytest.fixture
def corpus_settings(tmp_path: Path) -> Settings:
    return Settings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


@pytest.fixture
def full_corpus(corpus_settings: Settings) -> Settings:
    build_fixture(
        corpus_settings.health_data_dir,
        nights=3,
        heart_rate=True,
        hrv=True,
        sleep_score=82,
        resting_hr=True,
    )
    return corpus_settings


def test_this_iteration_serves_the_four_planned_metrics(corpus_settings: Settings) -> None:
    with GarminConnection(corpus_settings) as conn:
        assert set(metrics_for(conn)) == EXPECTED


def test_every_key_is_its_own_metric_id(corpus_settings: Settings) -> None:
    with GarminConnection(corpus_settings) as conn:
        for key, entry in metrics_for(conn).items():
            assert key == entry.descriptor.metric_id


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
def test_the_units_we_override(corpus_settings: Settings, metric_id: str, unit: str | None) -> None:
    with GarminConnection(corpus_settings) as conn:
        assert metrics_for(conn)[metric_id].descriptor.unit == unit


def test_every_entry_names_the_column_it_reads(corpus_settings: Settings) -> None:
    """Provenance is what makes a wrong number traceable to a table."""
    with GarminConnection(corpus_settings) as conn:
        for entry in metrics_for(conn).values():
            assert ".db:" in entry.provenance


def test_built_samples_are_never_interval_samples(full_corpus: Settings) -> None:
    with GarminConnection(full_corpus) as conn:
        for entry in metrics_for(conn).values():
            samples = entry.build(None, None, None)
            assert samples, entry.descriptor.metric_id
            assert not any(isinstance(s, IntervalSample) for s in samples)


def test_every_entry_builds_a_series_of_its_own_class(full_corpus: Settings) -> None:
    with GarminConnection(full_corpus) as conn:
        for entry in metrics_for(conn).values():
            series = entry.series(entry.build(None, None, None))
            assert isinstance(series, entry.series_cls)
            assert isinstance(series, TimeSeries)
            assert series.source == "garmin"


def test_probes_report_an_empty_corpus_as_having_nothing(corpus_settings: Settings) -> None:
    build_fixture(corpus_settings.health_data_dir, nights=0)
    with GarminConnection(corpus_settings) as conn:
        assert not any(entry.probe() for entry in metrics_for(conn).values())


def test_probes_report_exactly_what_the_corpus_holds(corpus_settings: Settings) -> None:
    build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
    with GarminConnection(corpus_settings) as conn:
        advertised = {k for k, e in metrics_for(conn).items() if e.probe()}
    assert advertised == {"heart_rate"}


def test_the_entries_are_bound_to_the_connection_they_were_built_for(
    corpus_settings: Settings,
) -> None:
    """Binding is what lets registry.py stay free of GarminConnection: the
    builder closes over the connection instead of being handed one."""
    build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
    with GarminConnection(corpus_settings) as conn:
        entry = metrics_for(conn)["heart_rate"]
        assert entry.build(None, None, 5)
