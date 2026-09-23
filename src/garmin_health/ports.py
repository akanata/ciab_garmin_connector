"""The ports a provider implements, and the vocabulary the core speaks.

A provider owns how data is acquired -- a polling scraper, a signed webhook, a
notification followed by a fetch -- along with the store it writes and the reader
over that store. What crosses this boundary is a ``health_data_service`` type, a
:class:`~garmin_health.registry.MetricEntry`, or a stdlib type. Never a database
handle, a session, or a vendor client.

The vocabulary here is deliberately not GarminDB's. ``LinkState`` has no
``AWAITING_MFA``, because an aggregator links by OAuth redirect and has no MFA
step to await; it has ``PENDING``, which both mean. ``LinkStatus.account`` is
whatever names the linked account -- an email here, an opaque user id elsewhere.
The MFA wording survives in ``detail``, which is the provider's to write.

``MetricEntry`` is imported only for type checking: ``registry.py`` imports
:data:`SOURCE` from here at runtime, so importing it back would be a cycle.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import runtime_checkable

import attrs
from health_data_service import SleepSession
from litestar import Litestar
from litestar.handlers import HTTPRouteHandler

if TYPE_CHECKING:
    from garmin_health.registry import MetricEntry

# The device the data came off, which is Garmin whichever way it reached us --
# scraped from Garmin Connect, or relayed by an aggregator that read the same
# watch. A consumer merging across providers keys on this, so it is one constant
# rather than a per-provider attribute that could quietly diverge.
SOURCE = "garmin"


class LinkState(StrEnum):
    """How far along the owner is in connecting their account."""

    NOT_LINKED = "not_linked"
    # Started but not finished: a code Garmin is waiting for, an OAuth redirect
    # the owner has not come back from. One state, because the page only needs to
    # know that something is in flight; what to *do* about it is in ``detail``.
    PENDING = "pending"
    LINKED = "linked"
    NEEDS_REAUTH = "needs_reauth"


@attrs.frozen
class LinkStatus:
    """The link state and the guidance that follows from it.

    ``detail`` must be derived from ``state`` alone, so that reading it is
    idempotent. A failure is **not** status: it is a one-shot flash, read through
    :meth:`AccountLink.take_error`. Anything sticky stored here is re-rendered on
    every later GET of ``/setup``, so one mistyped password would accuse the
    owner for ever.
    """

    state: LinkState
    account: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"state": self.state.value, "account": self.account, "detail": self.detail}


@attrs.frozen
class SetupView:
    """What the shell hands a provider when it renders its half of ``/setup``.

    Assembled once by the shell so that a provider never reaches into app state
    for it, and so the flash is consumed exactly once per page render.
    """

    link: LinkStatus
    # The consumed one-shot flash. Already rendered by the shell; here so a
    # provider can decide what to offer in light of it.
    error: str | None = None
    # ``HealthDataService.fault``: why serving is degraded, which is a different
    # failure from acquisition and is often the provider's to offer a fix for.
    serving_fault: str | None = None
    # A provider form's own validation message, re-rendered inline on a 400.
    provider_error: str | None = None
    today: dt.date = attrs.field(factory=dt.date.today)


@runtime_checkable
class HealthReader(Protocol):
    """Read access to whatever one provider has acquired.

    Deliberately small. There is no ``open`` (a provider hands back an already
    open reader) and no ``reset`` (pooled handles are the provider's problem, and
    a webhook provider has none). ``close`` is here only because the app's
    lifespan owns the reader's lifetime.
    """

    @property
    def fault(self) -> str | None:
        """Why this provider cannot serve, in words for the owner, or None."""

    @property
    def metrics(self) -> Mapping[str, MetricEntry]:
        """The catalog this provider contributes, already bound to its store."""

    def has_any_data(self) -> bool:
        """Whether anything at all has been acquired.

        Consulted only when the provider is not ready, to tell a container that
        has never acquired anything (serve empty, 200) from one holding data it
        cannot place on a real clock (503).
        """

    def sleep_sessions(
        self, start: dt.datetime | None, end: dt.datetime | None, limit: int
    ) -> list[SleepSession]:
        """Sessions starting in ``[start, end)``, newest first.

        ``limit`` has to bound the **work**, not just the answer: the spec's
        client sends a limit and no window, so an unbounded request over the
        whole store is the normal case.
        """

    def close(self) -> None: ...


@runtime_checkable
class AccountLink(Protocol):
    """The owner's connection to the upstream account.

    Only what the generic shell needs. Everything that *establishes* a link --
    credentials, an MFA code, a pasted token, an OAuth callback -- differs so
    completely between providers that it belongs to the provider's own routes.
    """

    def status(self) -> LinkStatus: ...

    def peek_error(self) -> str | None:
        """Read the pending failure without clearing it (for pollable endpoints)."""

    def take_error(self) -> str | None:
        """Read and clear the pending failure, so a refresh does not repeat it."""

    async def unlink(self) -> LinkStatus: ...


# What Litestar's ``lifespan=`` list accepts. A provider contributes its own --
# GarminDB a sync loop, a webhook provider perhaps a token refresher -- and they
# nest *inside* the app's serving lifespan, so the reader outlives them.
Lifespan = Callable[[Litestar], AbstractAsyncContextManager[None]]


@runtime_checkable
class Provider(Protocol):
    """One way of acquiring health data, and everything that follows from it.

    The app owns the HTTP contract, the serving rules and the owner page's shell;
    a provider owns acquisition, its own store, its own link flow and its own
    half of the page. Nothing here mentions polling: GarminDB schedules itself
    with a lifespan, a webhook provider would take a public route instead, and
    the app cannot tell the difference.
    """

    @property
    def name(self) -> str:
        """The configured id, as ``HEALTH_PROVIDER`` spells it."""

    @property
    def display_name(self) -> str:
        """What to call this on the owner's page, e.g. "Garmin Connect"."""

    @property
    def link(self) -> AccountLink: ...

    def open_reader(self) -> HealthReader:
        """Open a reader over the store. Called once, by the app's serving lifespan."""

    def lifespans(self) -> list[Lifespan]:
        """Background work to run for the life of the app."""

    def owner_routes(self) -> list[HTTPRouteHandler]:
        """Handlers to mount **under the owner guard**, at the router level."""

    def public_routes(self) -> list[HTTPRouteHandler]:
        """Ungated handlers, e.g. a webhook receiver.

        Every path here must also appear in ``openhost.toml``'s ``public_paths``,
        or the router will never forward to it.
        """

    async def status(self) -> dict[str, Any]:
        """Merged into ``/sync/status``.

        Must carry ``running`` (bool) and ``progress`` (a dict or None): the
        owner page's polling script is written against those two keys and is not
        provider-specific.
        """

    async def render_setup(self, view: SetupView) -> str:
        """This provider's HTML fragment, placed inside the shell."""

    def subscribe_data_changed(self, callback: Callable[[], None]) -> None:
        """Register a callback to fire after newly acquired data lands.

        The app uses it to drop the serving layer's catalog cache. A provider
        calls it after a sync, or after writing a webhook payload.
        """
