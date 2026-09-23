"""``HealthDataService``: the only thing ``routes/`` imports.

It owns the two policy decisions that are not obviously the routes' business:

**What the catalog advertises.** ``list_metrics_merged`` is how a consumer decides
what to request, so advertising a metric we hold no rows for costs it a wasted
round trip. Every entry is probed, behind a short TTL so a busy consumer does not
re-probe the whole catalog on each call.

**What a degraded provider answers.** There are two different degraded states and
they must not be conflated. A container that has never acquired anything holds no
data *and* has no clock to place it on, and the honest answer is an empty one -- a
brand-new install is not broken. A container that *has* data but cannot place it
on a real clock is a misconfiguration, and serving those rows anyway would shift
every timestamp by hours with nothing raising anywhere; that is a 503.

Both decisions are stated against :class:`~garmin_health.ports.HealthReader`, so
neither mentions GarminDB, SQLite, or a timezone.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from typing import Any

from health_data_service import MetricType
from health_data_service import SleepSession
from health_data_service import TimeSeries

from garmin_health.errors import ProviderNotReady
from garmin_health.errors import ProviderUnavailable
from garmin_health.limits import resolve_limit
from garmin_health.ports import HealthReader

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 60.0


class UnknownMetric(Exception):
    """No such metric_id in the provider's catalog. -> 404."""


class HealthDataService:
    """The serving facade over one :class:`~garmin_health.ports.HealthReader`."""

    def __init__(
        self,
        reader: HealthReader,
        *,
        clock: Callable[[], float] = time.monotonic,
        catalog_ttl: float = CATALOG_TTL_SECONDS,
    ) -> None:
        self._reader = reader
        self._clock = clock
        self._catalog_ttl = catalog_ttl
        self._catalog: list[MetricType] | None = None
        self._catalog_at = 0.0

    @property
    def fault(self) -> str | None:
        """Why the provider cannot serve, for the owner page. None when healthy."""
        return self._reader.fault

    def invalidate(self) -> None:
        """Drop the catalog cache. Called after an acquisition, which is the only
        thing that changes the answer."""
        self._catalog = None

    def _check_available(self) -> None:
        fault = self._reader.fault
        if fault is not None:
            raise ProviderUnavailable(fault)

    def _empty_if_nothing_acquired(self, exc: ProviderNotReady) -> None:
        """Re-raise unless there is genuinely nothing to serve.

        Deliberately not a blanket 'serve empty whenever the provider is not
        ready': that would turn a misconfigured container holding years of data
        into one that silently reports having none.

        The original exception is re-raised rather than wrapped, so the owner
        still gets the actionable message -- which zone to set, which secret is
        missing -- and the routes still see a :class:`ProviderUnavailable`.
        """
        if self._reader.has_any_data():
            raise exc
        logger.debug("The provider is not ready and holds nothing; serving empty results.")

    def metrics(self) -> list[MetricType]:
        """The catalog, filtered to metrics that actually hold data."""
        now = self._clock()
        if self._catalog is not None and now - self._catalog_at < self._catalog_ttl:
            return self._catalog

        catalog: list[MetricType] = []
        if self._reader.fault is None:
            try:
                catalog = [
                    entry.descriptor for entry in self._reader.metrics.values() if entry.probe()
                ]
            except ProviderUnavailable as exc:
                # An empty catalog is the true statement while degraded: there is
                # nothing here worth asking for.
                logger.warning("Could not probe the metric catalog: %s", exc)
                catalog = []

        self._catalog = catalog
        self._catalog_at = now
        return catalog

    def time_series(
        self,
        metric_id: str,
        start: dt.datetime | None,
        end: dt.datetime | None,
        limit: int | None,
    ) -> TimeSeries:
        """One metric's samples over ``[start, end)``.

        A known metric with no data in range is an empty series, never a 404: the
        consumer's ``_fan_out`` treats any non-200 as "this provider has nothing",
        which is the same outcome but loses the distinction for anyone debugging.
        """
        entry = self._reader.metrics.get(metric_id)
        if entry is None:
            raise UnknownMetric(metric_id)
        self._check_available()
        resolved = resolve_limit(limit)
        try:
            samples = entry.build(start, end, resolved)
        except ProviderNotReady as exc:
            self._empty_if_nothing_acquired(exc)
            samples = []
        return entry.series(samples)

    def sleep_sessions(
        self, start: dt.datetime | None, end: dt.datetime | None, limit: int | None
    ) -> list[SleepSession]:
        """Every sleep session starting in ``[start, end)``, newest first."""
        self._check_available()
        resolved = resolve_limit(limit)
        try:
            return self._reader.sleep_sessions(start, end, resolved)
        except ProviderNotReady as exc:
            self._empty_if_nothing_acquired(exc)
            return []

    def status(self) -> dict[str, Any]:
        """What the owner-facing endpoints report about the serving side.

        Only what this layer can answer for. How the provider stores anything --
        a database directory, a resolved timezone, a webhook cursor -- belongs to
        that provider's own status block.
        """
        return {
            "available": self._reader.fault is None,
            "fault": self._reader.fault,
            "metrics": [m.metric_id for m in self.metrics()],
        }
