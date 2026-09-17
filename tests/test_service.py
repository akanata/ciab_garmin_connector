"""The HealthDataService facade: catalog probing, windows, and degraded states.

Driven entirely by :class:`FakeReader`. These are the rules the serving layer
owes every consumer, and none of them depend on where the rows came from -- which
is why this suite no longer builds a SQLite corpus to state them.
"""

from __future__ import annotations

import datetime as dt

import pytest
from health_data_service import HeartRate

from garmin_health.config import DEFAULT_LIMIT
from garmin_health.errors import ProviderNotReady
from garmin_health.errors import ProviderUnavailable
from garmin_health.limits import InvalidLimit
from garmin_health.service import HealthDataService
from garmin_health.service import UnknownMetric
from tests.fakes import EPOCH
from tests.fakes import SAMPLE_COUNT
from tests.fakes import FakeReader
from tests.fakes import fake_samples


class TestCatalog:
    def test_it_advertises_only_metrics_that_hold_data(self) -> None:
        service = HealthDataService(FakeReader())
        assert [m.metric_id for m in service.metrics()] == ["heart_rate"]

    def test_a_metric_holding_nothing_is_not_advertised(self) -> None:
        """Advertising it costs the consumer a wasted round trip: the catalog is
        what list_metrics_merged uses to decide what to request."""
        service = HealthDataService(FakeReader(samples=[]))
        assert service.metrics() == []

    def test_the_catalog_is_cached_for_a_while(self) -> None:
        """Probing every metric on each /v1/metrics call would be wasteful, and
        the answer only changes when an acquisition lands."""
        service = HealthDataService(FakeReader())
        first = service.metrics()
        assert service.metrics() is first

    def test_the_cache_is_dropped_when_the_data_changes(self) -> None:
        reader = FakeReader()
        service = HealthDataService(reader)
        service.metrics()
        probes = reader.probe_calls
        service.invalidate()
        service.metrics()
        assert reader.probe_calls > probes

    def test_the_cache_expires_on_its_own(self) -> None:
        clock = [0.0]
        reader = FakeReader()
        service = HealthDataService(reader, clock=lambda: clock[0])
        service.metrics()
        probes = reader.probe_calls
        clock[0] += 3600
        service.metrics()
        assert reader.probe_calls > probes


class TestTimeSeries:
    def test_it_returns_the_metrics_own_spec_class(self) -> None:
        service = HealthDataService(FakeReader())
        series = service.time_series("heart_rate", None, None, 10)
        assert isinstance(series, HeartRate)
        assert series.unit == "bpm"
        assert len(series.samples) == SAMPLE_COUNT

    def test_an_unknown_metric_is_a_typed_error(self) -> None:
        """404, so the consumer's _fan_out reads it as 'this provider has nothing'."""
        service = HealthDataService(FakeReader())
        with pytest.raises(UnknownMetric):
            service.time_series("blood_glucose", None, None, None)

    def test_a_known_metric_with_no_data_in_range_is_an_empty_series_not_an_error(self) -> None:
        service = HealthDataService(FakeReader())
        far = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
        series = service.time_series("heart_rate", far, far + dt.timedelta(days=1), None)
        assert series.samples == []
        assert series.metric_id == "heart_rate"

    def test_the_window_is_passed_through_to_the_reader(self) -> None:
        service = HealthDataService(FakeReader())
        series = service.time_series("heart_rate", EPOCH, EPOCH + dt.timedelta(minutes=6), None)
        assert len(series.samples) == 3

    def test_an_absent_limit_becomes_the_default(self) -> None:
        reader = FakeReader(samples=fake_samples(DEFAULT_LIMIT + 10))
        service = HealthDataService(reader)
        assert len(service.time_series("heart_rate", None, None, None).samples) == DEFAULT_LIMIT

    def test_a_nonsense_limit_is_rejected(self) -> None:
        service = HealthDataService(FakeReader())
        with pytest.raises(InvalidLimit):
            service.time_series("heart_rate", None, None, 0)


class TestSleepSessions:
    def test_it_returns_what_the_reader_holds(self) -> None:
        service = HealthDataService(FakeReader())
        assert len(service.sleep_sessions(None, None, None)) == 1

    def test_an_empty_reader_is_an_empty_list(self) -> None:
        service = HealthDataService(FakeReader(sessions=[]))
        assert service.sleep_sessions(None, None, None) == []

    def test_the_limit_is_resolved_before_it_reaches_the_reader(self) -> None:
        """limit has to bound the work, not just the answer, so the reader is
        never handed None and left to decide for itself."""
        service = HealthDataService(FakeReader())
        with pytest.raises(InvalidLimit):
            service.sleep_sessions(None, None, 0)


class TestDegradedStates:
    """The two degraded states are different and must not be conflated."""

    def test_a_provider_that_has_never_acquired_anything_serves_empty(self) -> None:
        """Nothing has run, so there is no clock and nothing to serve anyway.
        Failing here would make a brand-new install look broken."""
        service = HealthDataService(FakeReader(ready=False, has_data=False))
        assert service.metrics() == []
        assert service.time_series("heart_rate", None, None, None).samples == []
        assert service.sleep_sessions(None, None, None) == []

    def test_a_populated_provider_that_is_not_ready_refuses_to_guess(self) -> None:
        """There IS data here, and serving it on the wrong clock would shift every
        timestamp by hours without anything raising. 503 with an actionable
        message beats a silent wrong answer that goes unnoticed for months."""
        service = HealthDataService(FakeReader(ready=False, has_data=True))
        with pytest.raises(ProviderUnavailable, match="GARMIN_HOME_TZ"):
            service.time_series("heart_rate", None, None, None)
        with pytest.raises(ProviderUnavailable):
            service.sleep_sessions(None, None, None)

    def test_the_original_not_ready_error_is_what_reaches_the_owner(self) -> None:
        """Re-raised rather than wrapped, so the actionable message survives."""
        service = HealthDataService(FakeReader(ready=False, has_data=True))
        with pytest.raises(ProviderNotReady):
            service.time_series("heart_rate", None, None, None)

    def test_a_faulted_provider_reports_unavailable(self) -> None:
        service = HealthDataService(FakeReader(fault="The schema must be rebuilt."))
        with pytest.raises(ProviderUnavailable, match="rebuilt"):
            service.time_series("heart_rate", None, None, None)
        with pytest.raises(ProviderUnavailable):
            service.sleep_sessions(None, None, None)

    def test_the_catalog_is_empty_rather_than_failing_when_faulted(self) -> None:
        """/v1/metrics is how a consumer decides what to ask for. An empty catalog
        says 'nothing to ask me for', which is exactly true while degraded."""
        service = HealthDataService(FakeReader(fault="The schema must be rebuilt."))
        assert service.metrics() == []

    def test_the_catalog_is_empty_rather_than_failing_when_not_ready(self) -> None:
        service = HealthDataService(FakeReader(ready=False))
        assert service.metrics() == []


class TestStatus:
    def test_it_reports_the_fault_for_the_owner(self) -> None:
        service = HealthDataService(FakeReader(fault="The schema must be rebuilt."))
        status = service.status()
        assert status["available"] is False
        assert status["fault"] == "The schema must be rebuilt."

    def test_it_is_clean_on_a_healthy_provider(self) -> None:
        status = HealthDataService(FakeReader()).status()
        assert status["available"] is True
        assert status["fault"] is None
        assert status["metrics"] == ["heart_rate"]

    def test_it_says_nothing_about_how_the_provider_stores_anything(self) -> None:
        """A database path or a timezone belongs to whichever provider has one;
        the serving layer reports only what it can answer for."""
        assert set(HealthDataService(FakeReader()).status()) == {"available", "fault", "metrics"}


def test_the_fault_is_readable_without_reaching_for_a_connection() -> None:
    """/setup shows the serving fault, and must not need a provider handle to."""
    assert HealthDataService(FakeReader(fault="broken")).fault == "broken"
