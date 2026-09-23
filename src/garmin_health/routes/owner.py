"""The owner surface's generic shell: the guard, /setup, and /sync/status.

The router strips any client-supplied ``X-OpenHost-*`` header before stamping its
own, so ``X-OpenHost-Is-Owner`` is trustworthy. These paths are also kept out of
the manifest's ``public_paths``, so this guard is the second of two locks.

Everything here is true of any provider. The forms that actually link an account,
and whatever a provider's acquisition needs the owner to decide, come from
``provider.owner_routes()`` and ``provider.render_setup()`` -- mounted on the
*same* router, so a provider handler cannot forget the guard.
"""

from __future__ import annotations

from typing import Any

from litestar import Router
from litestar import get
from litestar import post
from litestar.connection import ASGIConnection
from litestar.datastructures import State
from litestar.enums import MediaType
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler
from litestar.response import Redirect
from litestar.status_codes import HTTP_303_SEE_OTHER

from garmin_health.ports import Provider
from garmin_health.setup_page import provider_of
from garmin_health.setup_page import render_setup_page


def owner_guard(connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler) -> None:
    if connection.headers.get("x-openhost-is-owner") != "true":
        raise NotAuthorizedException()


@get(["/", "/setup"], media_type=MediaType.HTML)
async def setup_page(state: State) -> str:
    return await render_setup_page(state)


@get("/setup/status", sync_to_thread=False)
def setup_status(state: State) -> dict[str, str | None]:
    link = provider_of(state).link
    # peek, not take: this endpoint is pollable and must not steal the flash that
    # /setup still has to render.
    return {**link.status().as_dict(), "error": link.peek_error()}


@post("/setup/unlink", status_code=HTTP_303_SEE_OTHER)
async def unlink(state: State) -> Redirect:
    await provider_of(state).link.unlink()
    return Redirect("/setup", status_code=HTTP_303_SEE_OTHER)


@get("/sync/status")
async def sync_status(state: State) -> dict[str, object]:
    """What the provider is doing, plus the serving layer's own health.

    The two fail independently: a store can be perfectly fresh and still be
    unservable because its schema needs rebuilding, and that is a fault only the
    owner can clear -- so it has to be visible somewhere the owner looks.

    ``link_state`` is read from the provider's ``AccountLink`` here, which is its
    single source; ``provider.status()`` contributes ``running`` and ``progress``,
    which the page's polling script is written against.
    """
    provider = provider_of(state)
    service = state.get("health_service")
    return {
        "link_state": provider.link.status().state.value,
        **await provider.status(),
        "serving": service.status() if service is not None else {"available": False},
    }


def owner_router(provider: Provider) -> Router:
    """The shell's handlers and the provider's, behind one router-level guard."""
    return Router(
        path="/",
        route_handlers=[
            setup_page,
            setup_status,
            unlink,
            sync_status,
            *provider.owner_routes(),
        ],
        guards=[owner_guard],
    )
