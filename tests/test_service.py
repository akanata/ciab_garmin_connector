"""The HealthDataService facade: catalog probing, windows, and degraded states."""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from health_data_service import HeartRate

from garmin_health.config import DEFAULT_LIMIT
from garmin_health.config import Settings
from garmin_health.garmin.connection import GarminConnection
from garmin_health.garmin.connection import GarminUnavailable
from garmin_health.garmin.sampling import InvalidLimit
from garmin_health.service import HealthDataService
from garmin_health.service import UnknownMetric
from tests.fixtures import HOME_TZ_NAME
from tests.fixtures import build_fixture


@pytest.fixture
def corpus_settings(tmp_path: Path) -> Settings:
    return Settings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


def service_for(settings: Settings, **kwargs: object) -> HealthDataService:
    build_fixture(settings.health_data_dir, **kwargs)  # type: ignore[arg-type]
    return HealthDataService(GarminConnection(settings))


class TestCatalog:
    def test_it_advertises_only_metrics_that_hold_data(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        assert [m.metric_id for m in service.metrics()] == ["heart_rate"]

    def test_an_empty_corpus_advertises_nothing(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=0)
        assert service.metrics() == []

    def test_the_catalog_is_cached_for_a_while(self, corpus_settings: Settings) -> None:
        """Probing four tables on every /v1/metrics call would be wasteful, and the
        answer only changes when a sync lands."""
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        first = service.metrics()
        assert service.metrics() is first

    def test_the_cache_is_dropped_when_the_corpus_changes(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        assert [m.metric_id for m in service.metrics()] == ["heart_rate"]
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True, hrv=True)
        service.invalidate()
        assert "hrv_rmssd" in {m.metric_id for m in service.metrics()}

    def test_the_cache_expires_on_its_own(self, corpus_settings: Settings) -> None:
        clock = [0.0]
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        service = HealthDataService(GarminConnection(corpus_settings), clock=lambda: clock[0])
        assert [m.metric_id for m in service.metrics()] == ["heart_rate"]
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True, hrv=True)
        clock[0] += 3600
        assert "hrv_rmssd" in {m.metric_id for m in service.metrics()}


class TestTimeSeries:
    def test_it_returns_the_metrics_own_spec_class(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        series = service.time_series("heart_rate", None, None, 10)
        assert isinstance(series, HeartRate)
        assert series.unit == "bpm"
        assert len(series.samples) == 10

    def test_an_unknown_metric_is_a_typed_error(self, corpus_settings: Settings) -> None:
        """404, so the consumer's _fan_out reads it as 'this provider has nothing'."""
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        with pytest.raises(UnknownMetric):
            service.time_series("blood_glucose", None, None, None)

    def test_a_known_metric_with_no_data_is_an_empty_series_not_an_error(
        self, corpus_settings: Settings
    ) -> None:
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        far = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
        series = service.time_series("heart_rate", far, far + dt.timedelta(days=1), None)
        assert series.samples == []
        assert series.metric_id == "heart_rate"

    def test_a_metric_with_no_rows_at_all_is_still_answerable(
        self, corpus_settings: Settings
    ) -> None:
        """It is not advertised, but answering 404 for a metric the spec defines
        and we simply have no data for would be a lie about the catalog."""
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        assert service.time_series("hrv_rmssd", None, None, None).samples == []

    def test_an_absent_limit_becomes_the_default(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        assert len(service.time_series("heart_rate", None, None, None).samples) == min(
            240, DEFAULT_LIMIT
        )

    def test_a_nonsense_limit_is_rejected(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        with pytest.raises(InvalidLimit):
            service.time_series("heart_rate", None, None, 0)


class TestSleepSessions:
    def test_it_returns_sessions_newest_first(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=3)
        sessions = service.sleep_sessions(None, None, None)
        assert len(sessions) == 3
        assert [s.start for s in sessions] == sorted((s.start for s in sessions), reverse=True)

    def test_an_empty_corpus_is_an_empty_list(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=0)
        assert service.sleep_sessions(None, None, None) == []


class TestDegradedStates:
    def test_a_never_synced_container_serves_empty_rather_than_failing(
        self, tmp_path: Path
    ) -> None:
        """No sync has run, so the account's zone has not been imported and there
        is nothing to serve anyway. Failing here would make a brand-new install
        look broken."""
        settings = Settings(app_data_dir=tmp_path / "appdata")
        service = HealthDataService(GarminConnection(settings))
        assert service.metrics() == []
        assert service.time_series("heart_rate", None, None, None).samples == []
        assert service.sleep_sessions(None, None, None) == []

    def test_a_populated_corpus_with_no_resolvable_zone_refuses_to_guess(
        self, tmp_path: Path
    ) -> None:
        """There IS data here, and serving it on the container's local clock would
        shift every timestamp by hours without anything raising. 503 with an
        actionable message beats a silent wrong answer that goes unnoticed for
        months."""
        settings = Settings(app_data_dir=tmp_path / "appdata")
        build_fixture(settings.health_data_dir, nights=1, heart_rate=True, stored_time_zone=None)
        service = HealthDataService(GarminConnection(settings))
        with pytest.raises(GarminUnavailable, match="GARMIN_HOME_TZ"):
            service.time_series("heart_rate", None, None, None)
        with pytest.raises(GarminUnavailable):
            service.sleep_sessions(None, None, None)

    def test_a_stale_schema_reports_unavailable(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        with sqlite3.connect(corpus_settings.db_dir / "garmin.db") as db:
            db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")
        service = HealthDataService(GarminConnection(corpus_settings))

        with pytest.raises(GarminUnavailable, match="rebuild"):
            service.time_series("heart_rate", None, None, None)
        with pytest.raises(GarminUnavailable):
            service.sleep_sessions(None, None, None)

    def test_the_catalog_is_empty_rather_than_failing_when_degraded(
        self, corpus_settings: Settings
    ) -> None:
        """/v1/metrics is how a consumer decides what to ask for. An empty catalog
        says 'nothing to ask me for', which is exactly true while degraded."""
        build_fixture(corpus_settings.health_data_dir, nights=1, heart_rate=True)
        with sqlite3.connect(corpus_settings.db_dir / "garmin.db") as db:
            db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")
        service = HealthDataService(GarminConnection(corpus_settings))
        assert service.metrics() == []

    def test_status_reports_the_fault_for_the_owner(self, corpus_settings: Settings) -> None:
        build_fixture(corpus_settings.health_data_dir, nights=1)
        with sqlite3.connect(corpus_settings.db_dir / "garmin.db") as db:
            db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")
        service = HealthDataService(GarminConnection(corpus_settings))
        status = service.status()
        assert status["available"] is False
        assert status["fault"] is not None

    def test_status_is_clean_on_a_healthy_corpus(self, corpus_settings: Settings) -> None:
        service = service_for(corpus_settings, nights=1, heart_rate=True)
        status = service.status()
        assert status["available"] is True
        assert status["fault"] is None
        assert status["metrics"] == ["heart_rate"]
