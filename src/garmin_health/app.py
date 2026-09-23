"""Litestar application: the health probe, the owner surface, /v1/*, and wiring.

Three things are wired together here and nowhere else:

- The **reader** is opened once in a lifespan hook rather than at import, so
  nothing touches the data directory until the process is actually starting, and
  a degraded provider drops ``/v1/*`` to 503 without taking ``/health`` or
  ``/setup`` with it. It is opened first and closed last, so a provider's own
  lifespans nest inside it and their callbacks always find a service.
- **Data-changed notifications** drop the serving layer's catalog cache. The
  provider decides when that is -- after a sync here, after a webhook payload
  elsewhere -- and the app only has to subscribe.
- ``/health`` stays unconditional. An unlinked account, a stale corpus and an
  unreachable upstream are all normal states awaiting attention, and failing the
  probe for any of them makes the router restart a container that is working.

Nothing in this module names a provider. ``build_provider`` maps the configured
name to one; everything else here is stated against ``ports.Provider``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from litestar import Litestar
from litestar import Router
from litestar import get
from litestar.datastructures import State

from garmin_health.config import Settings
from garmin_health.config import settings_from_env
from garmin_health.ports import Provider
from garmin_health.providers import build_provider
from garmin_health.routes.owner import owner_router
from garmin_health.routes.service import v1_router

logger = logging.getLogger(__name__)


@get("/health", sync_to_thread=False)
def health() -> dict[str, str]:
    """The router's liveness probe.

    Deliberately unconditional: an unlinked account, a stale sync or an
    unreachable upstream are all normal states awaiting attention, and failing the
    probe for any of them would make the router restart a container that is
    working correctly.
    """
    return {"status": "ok"}


def create_app(*, settings: Settings | None = None, provider: Provider | None = None) -> Litestar:
    """Build the app.

    ``provider`` is injectable so tests can mount a double; left out, the
    configured one is built from the environment.
    """
    if settings is None:
        settings = settings_from_env()
    if provider is None:
        provider = build_provider(settings)
    state = State({"settings": settings, "provider": provider})

    def on_data_changed() -> None:
        """Drop the catalog cache after newly acquired data lands.

        The provider has already made the new data readable by the time this
        runs; all that is left is that the catalog was probed before it existed.
        """
        service = state.get("health_service")
        if service is not None:
            service.invalidate()

    provider.subscribe_data_changed(on_data_changed)

    @asynccontextmanager
    async def serving_layer(_: Litestar) -> AsyncGenerator[None, None]:
        # Imported here rather than at module scope so the reader's import cost is
        # paid at startup rather than at import, which keeps `create_app` cheap for
        # anything that only wants to inspect the routes.
        from garmin_health.service import HealthDataService  # noqa: PLC0415

        reader = provider.open_reader()
        state["health_reader"] = reader
        state["health_service"] = HealthDataService(reader)
        if reader.fault is not None:
            logger.error("Serving layer degraded: %s", reader.fault)
        try:
            yield
        finally:
            state.pop("health_service", None)
            state.pop("health_reader", None)
            reader.close()

    route_handlers: list[object] = [health, owner_router(provider), v1_router]
    public = provider.public_routes()
    if public:
        # A second, guard-less router. Only mounted when a provider actually has
        # ungated routes, so the app has no unguarded surface by default.
        route_handlers.append(Router(path="/", route_handlers=list(public)))

    return Litestar(
        route_handlers=route_handlers,  # type: ignore[arg-type]
        state=state,
        # The reader's lifespan is first, so the provider's own lifespans start
        # after it and are cancelled before it closes.
        lifespan=[serving_layer, *provider.lifespans()],
    )


logging.basicConfig(level=logging.INFO)

app = create_app()
