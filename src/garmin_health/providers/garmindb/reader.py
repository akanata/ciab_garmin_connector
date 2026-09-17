"""``GarminDbReader``: the corpus, seen through the generic reader port.

Everything the serving layer is allowed to know about GarminDB passes through
here. It is a thin adapter on purpose -- the queries live in ``sampling.py``,
``heart_rate.py``, ``daily.py`` and ``sleep.py``; what this adds is the shape
``service.py`` can depend on without naming any of them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

from health_data_service import SleepSession

from garmin_health.providers.garmindb.connection import GarminConnection
from garmin_health.providers.garmindb.registry import metrics_for
from garmin_health.providers.garmindb.sleep import build_sleep_sessions
from garmin_health.registry import MetricEntry


class GarminDbReader:
    """Reads one GarminDB corpus, over one :class:`GarminConnection`."""

    def __init__(self, connection: GarminConnection) -> None:
        self._conn = connection
        # Bound once: the entries close over this connection, and reset() swaps
        # handles inside it rather than replacing the object.
        self._metrics = metrics_for(connection)

    @property
    def connection(self) -> GarminConnection:
        """The underlying corpus handle. For this provider's own use only."""
        return self._conn

    @property
    def fault(self) -> str | None:
        return self._conn.fault

    @property
    def metrics(self) -> Mapping[str, MetricEntry]:
        return self._metrics

    def has_any_data(self) -> bool:
        return self._conn.has_any_data()

    def sleep_sessions(
        self, start: dt.datetime | None, end: dt.datetime | None, limit: int
    ) -> list[SleepSession]:
        return build_sleep_sessions(self._conn, start, end, limit)

    def close(self) -> None:
        self._conn.close()

    def reset(self) -> None:
        """Reopen the corpus. Not part of the port.

        After a rebuild the pooled handles point at a deleted inode and would go
        on serving stale rows silently; the account's timezone also only arrives
        with the first profile import, so the boot-time resolution has to be
        retried.
        """
        self._conn.reset()
