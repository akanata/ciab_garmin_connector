"""Doubles for the generic suite.

Nothing here imports a provider. That is the point: every degraded state the
serving layer has to tell apart is a constructor flag on :class:`FakeReader`, so
the rules can be tested without a database, a corpus, or a timezone.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any

from health_data_service import HeartRate
from health_data_service import Sample
from health_data_service import SleepSession
from litestar import get

from garmin_health.errors import ProviderNotReady
from garmin_health.ports import LinkState
from garmin_health.ports import LinkStatus
from garmin_health.ports import SetupView
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


class FakeLink:
    """An in-memory :class:`~garmin_health.ports.AccountLink`.

    The flash is the interesting part: ``peek`` must not consume it and ``take``
    must, or ``/setup/status`` polling would steal the message ``/setup`` still
    has to render.
    """

    def __init__(
        self,
        *,
        state: LinkState = LinkState.NOT_LINKED,
        account: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.state = state
        self.account = account
        self.detail = detail
        self._error: str | None = None
        self.unlink_calls = 0

    def plant_error(self, message: str) -> None:
        """Stand in for whatever failed upstream, without a failure to cause it."""
        self._error = message

    def status(self) -> LinkStatus:
        return LinkStatus(state=self.state, account=self.account, detail=self.detail)

    def peek_error(self) -> str | None:
        return self._error

    def take_error(self) -> str | None:
        error, self._error = self._error, None
        return error

    async def unlink(self) -> LinkStatus:
        self.unlink_calls += 1
        self.state = LinkState.NOT_LINKED
        return self.status()


@get("/fake/owner", sync_to_thread=False)
def fake_owner_route() -> dict[str, bool]:
    """Mounted under the app's owner guard, like any provider's own handler."""
    return {"owner": True}


@get("/fake/public", sync_to_thread=False)
def fake_public_route() -> dict[str, bool]:
    """Ungated, the way a webhook receiver would be."""
    return {"public": True}


class FakeProvider:
    """An in-memory :class:`~garmin_health.ports.Provider`.

    It acquires nothing and schedules nothing, which is the point: everything the
    app does around a provider -- mounting its routes under the guard, running its
    lifespans, closing its reader, dropping the catalog cache when it says data
    changed -- is testable without a store, a network, or a vendor library.
    """

    name = "fake"
    display_name = "Fake Health"

    def __init__(self, *, link: FakeLink | None = None, reader: FakeReader | None = None) -> None:
        self.link_double = link or FakeLink()
        self._reader = reader
        self.readers: list[FakeReader] = []
        self.subscribers: list[Callable[[], None]] = []
        self.setup_views: list[SetupView] = []
        # Appended to as the lifespan opens and closes, so a test can assert the
        # app both started and unwound it.
        self.lifespan_events: list[str] = []

    @property
    def link(self) -> FakeLink:
        return self.link_double

    def open_reader(self) -> FakeReader:
        reader = self._reader or FakeReader()
        self.readers.append(reader)
        return reader

    @asynccontextmanager
    async def _lifespan(self, _: Any) -> AsyncIterator[None]:
        self.lifespan_events.append("enter")
        try:
            yield
        finally:
            self.lifespan_events.append("exit")

    def lifespans(self) -> list[Any]:
        return [self._lifespan]

    def owner_routes(self) -> list[Any]:
        return [fake_owner_route]

    def public_routes(self) -> list[Any]:
        return [fake_public_route]

    async def status(self) -> dict[str, Any]:
        return {"running": False, "progress": None, "fake": True}

    async def render_setup(self, view: SetupView) -> str:
        self.setup_views.append(view)
        fault = view.serving_fault or ""
        error = view.provider_error or ""
        return (
            f"<p id='fake-setup' data-state='{view.link.state.value}' "
            f"data-fault='{fault}' data-provider-error='{error}'>fake setup</p>"
        )

    def subscribe_data_changed(self, callback: Callable[[], None]) -> None:
        self.subscribers.append(callback)

    def emit_data_changed(self) -> None:
        """Stand in for a finished sync, or a webhook payload landing."""
        for callback in self.subscribers:
            callback()
