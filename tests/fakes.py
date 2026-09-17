"""Doubles for the generic suite.

Nothing here imports a provider. That is the point: every degraded state the
serving layer has to tell apart is a constructor flag on :class:`FakeReader`, so
the rules can be tested without a database, a corpus, or a timezone.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from health_data_service import HeartRate
from health_data_service import Sample
from health_data_service import SleepSession

from garmin_health.errors import ProviderNotReady
from garmin_health.registry import MetricEntry
from garmin_health.registry import metric_entry

# An arbitrary but fixed evening, so a test can name an instant without a corpus.
EPOCH = dt.datetime(2026, 9, 14, 22, 0, tzinfo=dt.UTC)
SAMPLE_INTERVAL = dt.timedelta(minutes=2)
SAMPLE_COUNT = 10
SESSION_ID = "fake-night"

NOT_READY = "The fake provider cannot place its rows on a real clock. Set GARMIN_HOME_TZ."


def fake_samples(count: int = SAMPLE_COUNT) -> list[Sample[Any]]:
    return [
        Sample(timestamp=EPOCH + SAMPLE_INTERVAL * i, value=float(50 + i)) for i in range(count)
    ]


def fake_session() -> SleepSession:
    return SleepSession(
        source="garmin",
        id=SESSION_ID,
        start=EPOCH,
        end=EPOCH + dt.timedelta(hours=8),
    )


class FakeReader:
    """An in-memory :class:`~garmin_health.ports.HealthReader`.

    ``ready=False`` models a provider that cannot place its rows on a real clock
    yet -- GarminDB before its first profile import, an aggregator before its
    first payload. Combined with ``has_data`` it reaches both sides of the rule
    that decides between an empty 200 and a 503.
    """

    def __init__(
        self,
        *,
        fault: str | None = None,
        ready: bool = True,
        has_data: bool = True,
        samples: list[Sample[Any]] | None = None,
        sessions: list[SleepSession] | None = None,
    ) -> None:
        self.fault = fault
        self.ready = ready
        self._has_data = has_data
        self._samples = fake_samples() if samples is None else samples
        self._sessions = [fake_session()] if sessions is None else sessions
        self.probe_calls = 0
        self.closed = False
        self.metrics: Mapping[str, MetricEntry] = {
            "heart_rate": metric_entry(
                HeartRate,
                build=self._build,
                probe=self._probe,
                provenance="fake:heart_rate",
            )
        }

    def _require_ready(self) -> None:
        if not self.ready:
            raise ProviderNotReady(NOT_READY)

    def _probe(self) -> bool:
        self.probe_calls += 1
        self._require_ready()
        return bool(self._samples)

    def _build(
        self, start: dt.datetime | None, end: dt.datetime | None, limit: int | None
    ) -> list[Sample[Any]]:
        self._require_ready()
        chosen = [
            s
            for s in self._samples
            if (start is None or s.timestamp >= start) and (end is None or s.timestamp < end)
        ]
        return chosen if limit is None else chosen[:limit]

    def has_any_data(self) -> bool:
        return self._has_data

    def sleep_sessions(
        self, start: dt.datetime | None, end: dt.datetime | None, limit: int
    ) -> list[SleepSession]:
        self._require_ready()
        chosen = [
            s
            for s in self._sessions
            if (start is None or s.start >= start) and (end is None or s.start < end)
        ]
        return chosen[:limit]

    def close(self) -> None:
        self.closed = True
