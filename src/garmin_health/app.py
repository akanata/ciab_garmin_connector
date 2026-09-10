"""Litestar application: the health probe, the owner setup surface, and the sync loop.

The ``/v1/*`` spec surface is not implemented yet (it is the serving layer, and it
needs the GarminDB mapping work). The manifest already advertises the service, which
is safe: the spec's consumer client treats any non-200 from a provider as "this
provider has nothing", so a consumer routed here simply sees no data.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from collections.abc import Callable
from contextlib import asynccontextmanager

from litestar import Litestar
from litestar import get
from litestar.datastructures import State

from garmin_health.auth import GarminAuthenticator
from garmin_health.config import Settings
from garmin_health.config import settings_from_env
from garmin_health.routes.owner import owner_router
from garmin_health.sync import Ingest
from garmin_health.sync import SyncEngine

logger = logging.getLogger(__name__)

# How long shutdown waits for the loop task to unwind. An in-flight download blocks
# in time.sleep and cannot be interrupted, so it is abandoned rather than awaited;
# GarminDB commits per file and the retained JSON/FIT corpus makes a partial import
# re-runnable without re-downloading.
SHUTDOWN_GRACE_SECONDS = 5.0


@get("/health", sync_to_thread=False)
def health() -> dict[str, str]:
    """The router's liveness probe.

    Deliberately unconditional: an unlinked account, a stale sync or an
    unreachable Garmin are all normal states awaiting attention, and failing the
    probe for any of them would make the router restart a container that is
    working correctly.
    """
    return {"status": "ok"}


def _default_ingest_factory(settings: Settings) -> Callable[[], Ingest]:
    def build() -> Ingest:
        # Imported lazily so a container that has never linked an account does not
        # pay GarminDB's import cost, and so nothing touches the config directory
        # until there is actually a sync to run.
        from garmin_health.garmin.ingest import GarminDbIngest  # noqa: PLC0415

        return GarminDbIngest(settings)

    return build


def create_app(
    *,
    settings: Settings | None = None,
    authenticator: GarminAuthenticator | None = None,
    ingest_factory: Callable[[], Ingest] | None = None,
) -> Litestar:
    settings = settings if settings is not None else settings_from_env()
    authenticator = authenticator if authenticator is not None else GarminAuthenticator(settings)
    engine = SyncEngine(
        settings=settings,
        authenticator=authenticator,
        ingest_factory=ingest_factory or _default_ingest_factory(settings),
    )

    @asynccontextmanager
    async def sync_loop(_: Litestar) -> AsyncGenerator[None, None]:
        task = asyncio.create_task(engine.run_forever())
        try:
            yield
        finally:
            engine.request_stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=SHUTDOWN_GRACE_SECONDS)

    return Litestar(
        route_handlers=[health, owner_router],
        state=State({"settings": settings, "authenticator": authenticator, "sync_engine": engine}),
        lifespan=[sync_loop],
    )


logging.basicConfig(level=logging.INFO)

app = create_app()
