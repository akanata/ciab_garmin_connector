"""``HealthDataService``: the only thing ``routes/`` imports.

It owns the two policy decisions that are not obviously the routes' business:

**What the catalog advertises.** ``list_metrics_merged`` is how a consumer decides
what to request, so advertising a metric we hold no rows for costs it a wasted
round trip. Every entry is probed, behind a short TTL so a busy consumer does not
re-probe four tables per call.

**What a degraded corpus answers.** There are two different degraded states and
they must not be conflated. A container that has never synced has no data *and* no
resolvable timezone, and the honest answer is an empty one -- a brand-new install
is not broken. A container that *has* data but cannot resolve a timezone is a
misconfiguration, and serving those rows on the container's local clock would shift
every timestamp by hours with nothing raising anywhere; that is a 503.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

from health_data_service import MetricType
from health_data_service import SleepSession
from health_data_service import TimeSeries

from garmin_health.garmin.connection import GarminConnection
from garmin_health.garmin.connection import GarminUnavailable
from garmin_health.garmin.sampling import resolve_limit
from garmin_health.garmin.sleep import build_sleep_sessions
from garmin_health.registry import METRICS
from garmin_health.registry import MetricEntry
from garmin_health.timezones import TimeZoneUnresolved

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 60.0


class UnknownMetric(Exception):
    """No such metric_id in the registry. -> 404."""


class HealthDataService:
    """The serving facade over one :class:`GarminConnection`."""

    def __init__(
        self,
        connection: GarminConnection,
        *,
        metrics: Mapping[str, MetricEntry] = METRICS,
        clock: Callable[[], float] = time.monotonic,
        catalog_ttl: float = CATALOG_TTL_SECONDS,
    ) -> None:
        self._conn = connection
        self._metrics = metrics
        self._clock = clock
        self._catalog_ttl = catalog_ttl
        self._catalog: list[MetricType] | None = None
        self._catalog_at = 0.0

    @property
    def connection(self) -> GarminConnection:
        return self._conn

    def invalidate(self) -> None:
        """Drop the catalog cache. Called after a sync, which is the only thing
        that changes the answer."""
        self._catalog = None

    def _check_available(self) -> None:
        if self._conn.fault is not None:
            raise GarminUnavailable(self._conn.fault)

    def _empty_if_never_synced(self, exc: TimeZoneUnresolved) -> None:
        """Re-raise as unavailable unless there is genuinely nothing to serve.

        Deliberately not a blanket 'serve empty on any tz failure': that would turn
        a misconfigured container holding years of data into one that silently
        reports having none.
        """
        if self._conn.has_any_data():
            raise GarminUnavailable(str(exc)) from exc
        logger.debug("No timezone and no data yet; serving empty results.")

    def metrics(self) -> list[MetricType]:
        """The catalog, filtered to metrics that actually hold rows."""
        now = self._clock()
        if self._catalog is not None and now - self._catalog_at < self._catalog_ttl:
            return self._catalog

        catalog: list[MetricType] = []
        if self._conn.fault is None:
            try:
                catalog = [
                    entry.descriptor for entry in self._metrics.values() if entry.probe(self._conn)
                ]
            except (GarminUnavailable, TimeZoneUnresolved) as exc:
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
        entry = self._metrics.get(metric_id)
        if entry is None:
            raise UnknownMetric(metric_id)
        self._check_available()
        resolved = resolve_limit(limit)
        try:
            samples = entry.build(self._conn, start, end, resolved)
        except TimeZoneUnresolved as exc:
            self._empty_if_never_synced(exc)
            samples = []
        return entry.series(samples)

    def sleep_sessions(
        self, start: dt.datetime | None, end: dt.datetime | None, limit: int | None
    ) -> list[SleepSession]:
        """Every sleep session starting in ``[start, end)``, newest first."""
        self._check_available()
        resolved = resolve_limit(limit)
        try:
            return build_sleep_sessions(self._conn, start, end, resolved)
        except TimeZoneUnresolved as exc:
            self._empty_if_never_synced(exc)
            return []

    def status(self) -> dict[str, Any]:
        """What the owner-facing endpoints report about the serving side."""
        return {
            "available": self._conn.fault is None,
            "metrics": [m.metric_id for m in self.metrics()],
            **self._conn.status(),
        }
