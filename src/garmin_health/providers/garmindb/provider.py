"""``GarminDbProvider``: acquisition by driving GarminDB, behind the generic port.

This is the object ``app.py`` holds. Everything scraper-shaped lives behind it --
the sync engine and its schedule, the Garmin sign-in, the SQLite corpus, the
timezone policy -- and none of it reaches the app, which knows only
:class:`~garmin_health.ports.Provider`.

The polling *is* the provider's, and that is the whole point: the schedule is a
lifespan this object contributes, not a loop the app runs. A webhook provider
would contribute no lifespan and a public route instead, and ``app.py`` would not
change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from collections.abc import Callable
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from litestar import Litestar
from litestar.handlers import HTTPRouteHandler

from garmin_health.ports import AccountLink
from garmin_health.ports import HealthReader
from garmin_health.ports import Lifespan
from garmin_health.ports import SetupView
from garmin_health.providers.garmindb import owner
from garmin_health.providers.garmindb.auth import GarminAuthenticator
from garmin_health.providers.garmindb.preferences import load_preferences
from garmin_health.providers.garmindb.reader import GarminDbReader
from garmin_health.providers.garmindb.settings import GarminDbSettings
from garmin_health.providers.garmindb.sync import Ingest
from garmin_health.providers.garmindb.sync import SyncEngine

logger = logging.getLogger(__name__)

# How long shutdown waits for the loop task to unwind. An in-flight download blocks
# in time.sleep and cannot be interrupted, so it is abandoned rather than awaited;
# GarminDB commits per file and the retained JSON/FIT corpus makes a partial import
# re-runnable without re-downloading.
SHUTDOWN_GRACE_SECONDS = 5.0


def _default_ingest_factory(settings: GarminDbSettings) -> Callable[[], Ingest]:
    def build() -> Ingest:
        # Imported lazily so nothing touches the config directory until there is
        # actually a sync to run.
        from garmin_health.providers.garmindb.ingest import GarminDbIngest  # noqa: PLC0415

        return GarminDbIngest(settings)

    return build


class GarminDbProvider:
    """Acquires health data by driving GarminDB on a schedule."""

    name = "garmindb"
    display_name = "Garmin Connect"

    def __init__(
        self,
        settings: GarminDbSettings,
        *,
        authenticator: GarminAuthenticator | None = None,
        ingest_factory: Callable[[], Ingest] | None = None,
    ) -> None:
        self._settings = settings
        self._authenticator = authenticator or GarminAuthenticator(settings)
        self._reader: GarminDbReader | None = None
        self._subscribers: list[Callable[[], None]] = []
        self._engine = SyncEngine(
            settings=settings,
            authenticator=self._authenticator,
            ingest_factory=ingest_factory or _default_ingest_factory(settings),
            on_corpus_changed=self._corpus_changed,
            # The owner's saved interval, read fresh each time the loop decides
            # when to sync next; SYNC_INTERVAL_SECONDS only seeds it.
            interval=lambda: load_preferences(settings).sync_interval_seconds,
        )

    @classmethod
    def from_env(cls, app_data_dir: Path, env: Mapping[str, str] | None = None) -> GarminDbProvider:
        return cls(GarminDbSettings.from_env(app_data_dir, env))

    # -- provider-only accessors, for this package's own handlers --------------

    @property
    def settings(self) -> GarminDbSettings:
        return self._settings

    @property
    def authenticator(self) -> GarminAuthenticator:
        """The concrete authenticator, with the login flows the port does not carry."""
        return self._authenticator

    @property
    def engine(self) -> SyncEngine:
        return self._engine

    # -- the port -------------------------------------------------------------

    @property
    def link(self) -> AccountLink:
        return self._authenticator

    def open_reader(self) -> HealthReader:
        """Open the corpus. Called once, by the app's serving lifespan.

        The connection is retained so that :meth:`_corpus_changed` can reset it:
        after a rebuild, pooled handles point at the deleted inode and would go on
        serving stale rows silently.
        """
        # Imported here rather than at module scope so the GarminDB import cost is
        # paid at startup rather than at import, which keeps building the app cheap
        # for anything that only wants to inspect the routes.
        from garmin_health.providers.garmindb.connection import GarminConnection  # noqa: PLC0415

        reader = GarminDbReader(GarminConnection(self._settings))
        self._reader = reader
        return reader

    def lifespans(self) -> list[Lifespan]:
        return [self._sync_loop]

    def owner_routes(self) -> list[HTTPRouteHandler]:
        return list(owner.OWNER_HANDLERS)

    def public_routes(self) -> list[HTTPRouteHandler]:
        """None. Nothing here is reached without the owner header.

        A webhook provider would return its receiver here, and would have to add
        that path to ``openhost.toml``'s ``public_paths``.
        """
        return []

    async def status(self) -> dict[str, Any]:
        status: dict[str, Any] = self._engine.status()
        status["coverage"] = [c.as_dict() for c in (await self._engine.coverage()).values()]
        # Where the corpus is and what clock it is on. Provider-specific by
        # definition, so it sits under its own key rather than in the generic
        # serving block.
        status["corpus"] = self._reader.connection.status() if self._reader is not None else None
        return status

    async def render_setup(self, view: SetupView) -> str:
        return await owner.render_fragment(self, view)

    def subscribe_data_changed(self, callback: Callable[[], None]) -> None:
        self._subscribers.append(callback)

    # -- internals ------------------------------------------------------------

    def _corpus_changed(self) -> None:
        """Called on the event loop after every sync, whatever the outcome.

        Resets our own connection *first*: the account's timezone only arrives
        with the first profile import, and after a rebuild the old handles point
        at a deleted inode. Only then are subscribers told, so a subscriber that
        re-reads the corpus sees the new one.
        """
        if self._reader is not None:
            self._reader.reset()
        for callback in self._subscribers:
            callback()

    @asynccontextmanager
    async def _sync_loop(self, _: Litestar) -> AsyncGenerator[None, None]:
        task = asyncio.create_task(self._engine.run_forever())
        try:
            yield
        finally:
            self._engine.request_stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=SHUTDOWN_GRACE_SECONDS)
