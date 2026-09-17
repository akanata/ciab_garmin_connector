"""Litestar application: the health probe, the owner surface, /v1/*, and the sync loop.

Three things are wired together here and nowhere else:

- The **GarminConnection** is opened once in a lifespan hook rather than at import,
  so nothing touches the data directory until the process is actually starting, and
  a stale schema degrades ``/v1/*`` to 503 without taking ``/health`` or ``/setup``
  with it.
- The **sync engine** is given a callback that resets that connection. After a
  rebuild, pooled handles point at the deleted inode and would go on serving stale
  rows silently; and the account's timezone only arrives with the first profile
  import, so the boot-time resolution legitimately has to be retried.
- ``/health`` stays unconditional. An unlinked account, a stale corpus and an
  unreachable Garmin are all normal states awaiting attention, and failing the
  probe for any of them makes the router restart a container that is working.
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
from garmin_health.preferences import load_preferences
from garmin_health.routes.owner import owner_router
from garmin_health.routes.service import v1_router
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
        # Imported lazily so nothing touches the config directory until there is
        # actually a sync to run.
        from garmin_health.providers.garmindb.ingest import GarminDbIngest  # noqa: PLC0415

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
    state = State({"settings": settings, "authenticator": authenticator})

    def on_corpus_changed() -> None:
        """Called after every sync, on the event loop.

        Cheap (two ``engine.dispose()`` calls and two DB constructions) and the
        only thing that makes a rebuilt corpus, or a timezone that arrived with the
        first profile import, visible to the serving layer.
        """
        reader = state.get("health_reader")
        if reader is not None:
            reader.reset()
        service = state.get("health_service")
        if service is not None:
            service.invalidate()

    engine = SyncEngine(
        settings=settings,
        authenticator=authenticator,
        ingest_factory=ingest_factory or _default_ingest_factory(settings),
        on_corpus_changed=on_corpus_changed,
        # The owner's saved interval, read fresh each time the loop decides when to
        # sync next; SYNC_INTERVAL_SECONDS only seeds it.
        interval=lambda: load_preferences(settings).sync_interval_seconds,
    )
    state["sync_engine"] = engine

    @asynccontextmanager
    async def serving_layer(_: Litestar) -> AsyncGenerator[None, None]:
        # Imported here rather than at module scope so the GarminDB import cost is
        # paid at startup rather than at import, which keeps `create_app` cheap for
        # anything that only wants to inspect the routes.
        from garmin_health.providers.garmindb.connection import GarminConnection  # noqa: PLC0415
        from garmin_health.providers.garmindb.reader import GarminDbReader  # noqa: PLC0415
        from garmin_health.service import HealthDataService  # noqa: PLC0415

        reader = GarminDbReader(GarminConnection(settings))
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
        route_handlers=[health, owner_router, v1_router],
        state=state,
        lifespan=[serving_layer, sync_loop],
    )


logging.basicConfig(level=logging.INFO)

app = create_app()
