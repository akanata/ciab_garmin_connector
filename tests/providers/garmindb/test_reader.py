"""``GarminDbReader``: the corpus seen through the generic reader port.

Everything the serving layer is allowed to know about GarminDB passes through
here, so these tests are the proof that the port's five members are enough.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from health_data_service import SleepSession

from garmin_health.errors import ProviderNotReady
from garmin_health.ports import HealthReader
from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.reader import GarminDbReader
from garmin_health.providers.garmindb.settings import GarminDbSettings
from tests.providers.garmindb.fixtures import build_fixture


def reader_for(settings: GarminDbSettings, **kwargs: object) -> GarminDbReader:
    build_fixture(settings.health_data_dir, **kwargs)  # type: ignore[arg-type]
    return GarminDbReader(GarminConnection(settings))


def test_it_satisfies_the_reader_port(corpus_settings: GarminDbSettings) -> None:
    reader = reader_for(corpus_settings, nights=1, heart_rate=True)
    assert isinstance(reader, HealthReader)


def test_it_offers_the_catalog_bound_to_its_own_corpus(corpus_settings: GarminDbSettings) -> None:
    reader = reader_for(corpus_settings, nights=1, heart_rate=True)
    assert {k for k, e in reader.metrics.items() if e.probe()} == {"heart_rate"}


def test_it_builds_sessions_newest_first(corpus_settings: GarminDbSettings) -> None:
    reader = reader_for(corpus_settings, nights=3)
    sessions = reader.sleep_sessions(None, None, 10)
    assert len(sessions) == 3
    assert all(isinstance(s, SleepSession) for s in sessions)
    assert [s.start for s in sessions] == sorted((s.start for s in sessions), reverse=True)


def test_the_session_limit_bounds_the_work(corpus_settings: GarminDbSettings) -> None:
    reader = reader_for(corpus_settings, nights=3)
    assert len(reader.sleep_sessions(None, None, 1)) == 1


def test_a_healthy_corpus_has_no_fault(corpus_settings: GarminDbSettings) -> None:
    assert reader_for(corpus_settings, nights=1, heart_rate=True).fault is None


def test_an_empty_corpus_reports_holding_nothing(corpus_settings: GarminDbSettings) -> None:
    assert reader_for(corpus_settings, nights=0).has_any_data() is False


def test_a_populated_corpus_reports_holding_something(corpus_settings: GarminDbSettings) -> None:
    assert reader_for(corpus_settings, nights=1, heart_rate=True).has_any_data() is True


def test_a_stale_schema_surfaces_as_a_fault(corpus_settings: GarminDbSettings) -> None:
    build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
    with sqlite3.connect(corpus_settings.db_dir / "garmin.db") as db:
        db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")
    reader = GarminDbReader(GarminConnection(corpus_settings))
    assert reader.fault is not None
    assert "rebuilt" in reader.fault


def test_an_unresolvable_timezone_is_a_provider_that_is_not_ready(tmp_path: Path) -> None:
    """The reader does not decide what that means -- service.py does, from whether
    has_any_data() is true. It only has to raise the right kind of error."""
    settings = GarminDbSettings(app_data_dir=tmp_path / "appdata")
    build_fixture(settings.health_data_dir, nights=1, heart_rate=True, stored_time_zone=None)
    reader = GarminDbReader(GarminConnection(settings))
    with pytest.raises(ProviderNotReady, match="GARMIN_HOME_TZ"):
        reader.sleep_sessions(None, None, 10)


def test_closing_it_closes_the_corpus(corpus_settings: GarminDbSettings) -> None:
    reader = reader_for(corpus_settings, nights=1, heart_rate=True)
    reader.close()
    assert reader.has_any_data() is False
