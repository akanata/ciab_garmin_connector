"""The GarminDB provider's failures, seen as the generic ones.

``routes/service.py`` maps :class:`ProviderUnavailable` to 503 and imports
nothing from this package, so these subclass relationships are the whole reason
a GarminDB fault still reaches a consumer as 503 rather than 500.
"""

from __future__ import annotations

import pytest

from garmin_health.errors import ProviderNotReady
from garmin_health.errors import ProviderUnavailable
from garmin_health.providers.garmindb.connection import GarminSchemaMismatch
from garmin_health.providers.garmindb.connection import GarminUnavailable
from garmin_health.providers.garmindb.timezones import TimeZoneUnresolved


def test_an_unservable_corpus_is_a_provider_that_is_unavailable() -> None:
    assert issubclass(GarminUnavailable, ProviderUnavailable)


def test_a_stale_schema_is_still_an_unavailable_corpus() -> None:
    assert issubclass(GarminSchemaMismatch, GarminUnavailable)


def test_an_unresolved_timezone_is_a_provider_that_is_not_ready() -> None:
    """This is the one that decides empty-versus-503, so it must land on the
    not-ready branch rather than the general one."""
    assert issubclass(TimeZoneUnresolved, ProviderNotReady)


def test_a_stale_schema_is_not_treated_as_never_synced() -> None:
    """A rebuildable corpus holds data. Serving empty for it would report having
    no history to a consumer that could otherwise have waited."""
    assert not issubclass(GarminSchemaMismatch, ProviderNotReady)


def test_a_fault_raised_by_the_provider_is_catchable_generically() -> None:
    with pytest.raises(ProviderUnavailable, match="rebuilt"):
        raise GarminSchemaMismatch("The schema on disk must be rebuilt.")
