"""Owner-facing setup surface: /setup and its form posts.

The router strips any client-supplied ``X-OpenHost-*`` header before stamping its
own, so ``X-OpenHost-Is-Owner`` is trustworthy. These paths are also kept out of
the manifest's ``public_paths``, so this guard is the second of two locks.

The only HTML in the project is here, and it is plain server-rendered markup: this
is a JSON API with one setup page, not a frontend.
"""

from __future__ import annotations

from html import escape
from typing import Annotated
from typing import Any

from litestar import Router
from litestar import get
from litestar import post
from litestar.connection import ASGIConnection
from litestar.datastructures import State
from litestar.enums import MediaType
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.exceptions import NotAuthorizedException
from litestar.handlers.base import BaseRouteHandler
from litestar.params import Body
from litestar.response import Redirect
from litestar.status_codes import HTTP_202_ACCEPTED
from litestar.status_codes import HTTP_303_SEE_OTHER
from litestar.status_codes import HTTP_409_CONFLICT

from garmin_health.auth import AuthError
from garmin_health.auth import AuthStatus
from garmin_health.auth import GarminAuthenticator
from garmin_health.auth import LinkState
from garmin_health.sync import SyncEngine

PASSWORD_NOTICE = (
    "Garmin does not offer a consent-based API outside its developer portal, so linking "
    "replays your real Garmin Connect password against Garmin's sign-in form once. It is "
    "used for that request only, is never written to disk, and is discarded as soon as a "
    "token is saved. Only you, the owner of this bottle, can reach this page."
)


def owner_guard(connection: ASGIConnection[Any, Any, Any, Any], _: BaseRouteHandler) -> None:
    if connection.headers.get("x-openhost-is-owner") != "true":
        raise NotAuthorizedException()


def _authenticator(state: State) -> GarminAuthenticator:
    authenticator: GarminAuthenticator = state.authenticator
    return authenticator


def _sync_engine(state: State) -> SyncEngine:
    engine: SyncEngine = state.sync_engine
    return engine


def _describe_last_sync(engine: SyncEngine) -> str:
    """One line of sync state for the owner, in plain words."""
    status = engine.status()
    if status["running"]:
        return "A sync is running now."
    last = status["last_sync"]
    if last is None:
        return "Not synced yet."
    when = escape(str(last["finished_at"]))
    if last["error"]:
        return f"Last sync failed during {escape(str(last['phase']))} at {when}: {escape(str(last['error']))}"
    rows = sum(table["rows"] for table in last["tables"].values())
    if not last["changed"]:
        return f"Last sync finished at {when} and added nothing new ({rows} rows held)."
    return f"Last sync finished at {when} ({rows} rows held)."


def _serving_fault(state: State) -> str | None:
    """The serving layer's fault, if it has one.

    Sync health and serving health fail independently: a corpus can be perfectly
    fresh and still be unservable because its schema needs rebuilding. That is a
    fault only the owner can clear, so it has to appear where the owner looks.
    """
    service = state.get("health_service")
    if service is None:
        return None
    fault: str | None = service.connection.fault
    return fault


# Progressive enhancement only. The plain form POST is what submits; this just
# reports that the (multi-second, real) Garmin SSO round-trip is under way. If a
# CSP blocks the inline script, linking still works with no feedback.
BUSY_SCRIPT = (
    "<script>"
    "document.addEventListener('submit',function(e){"
    "var f=e.target,m=f.getAttribute('data-busy');if(!m)return;"
    "if(f.dataset.sent){e.preventDefault();return;}"  # a second click would start a second login
    "f.dataset.sent='1';"
    "var b=f.querySelector('button[type=submit]');"
    "if(b){b.classList.add('is-busy');b.disabled=true;}"
    "var s=document.getElementById('busy');"
    "if(s){s.textContent=m;s.hidden=false;}"
    "});"
    # A bfcache back-navigation restores the DOM as it was left, which would
    # otherwise show a spinner over a permanently disabled button.
    "window.addEventListener('pageshow',function(ev){"
    "if(!ev.persisted)return;"
    "document.querySelectorAll('form[data-busy]').forEach(function(f){"
    "delete f.dataset.sent;"
    "var b=f.querySelector('button[type=submit]');"
    "if(b){b.classList.remove('is-busy');b.disabled=false;}"
    "});"
    "var s=document.getElementById('busy');if(s){s.hidden=true;}"
    "});"
    "</script>"
)

STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;margin:0 auto;padding:2rem;max-width:34rem}"
    "label{display:block;margin:.75rem 0}input{display:block;width:100%;padding:.5rem;margin-top:.25rem}"
    "button{padding:.5rem 1rem;margin-top:.5rem}"
    "[hidden]{display:none!important}"
    ".error{padding:.75rem;background:#fdecea;border-left:3px solid #d32f2f}"
    ".detail{padding:.75rem;background:#fff4e5;border-left:3px solid #d97706}"
    ".warning,.note{color:#555;font-size:.875rem}"
    ".busy{color:#555;font-size:.875rem;margin-top:.5rem}"
    ".sync{padding:.75rem;background:#eef6ff;border-left:3px solid #2563eb}"
    ".spinner{display:none;width:.85em;height:.85em;margin-right:.5em;vertical-align:-.1em;"
    "border:2px solid currentColor;border-right-color:transparent;border-radius:50%;"
    "animation:spin .7s linear infinite}"
    "button.is-busy .spinner{display:inline-block}"
    "button.is-busy{opacity:.7;cursor:progress}"
    "@keyframes spin{to{transform:rotate(360deg)}}"
    "@media (prefers-reduced-motion:reduce){.spinner{animation-duration:2.4s}}"
)


def _button(label: str) -> str:
    return f"<button type='submit'><span class='spinner' aria-hidden='true'></span>{escape(label)}</button>"


def _render(
    status: AuthStatus,
    error: str | None = None,
    sync_summary: str = "",
    serving_fault: str | None = None,
) -> str:
    email = escape(status.email or "", quote=True)

    if status.state is LinkState.LINKED:
        headline = f"Linked to Garmin Connect as {email}." if email else "Linked to Garmin Connect."
    elif status.state is LinkState.AWAITING_MFA:
        headline = "Garmin sent a verification code."
    elif status.state is LinkState.NEEDS_REAUTH:
        headline = "This bottle is no longer linked to Garmin Connect."
    else:
        headline = "Not linked to Garmin Connect yet."

    body = [
        "<h1>Garmin Connect</h1>",
        f"<p class='state' data-state='{status.state.value}'>{escape(headline)}</p>",
    ]
    if error:
        body.append(f"<p class='error' role='alert'>{escape(error)}</p>")
    if status.detail:
        body.append(f"<p class='detail'>{escape(status.detail)}</p>")
    if serving_fault:
        # Offered only while there is actually a fault: a destructive action
        # should not sit on the page when nothing is wrong.
        body.append(
            f"<p class='detail'>{escape(serving_fault)}</p>"
            "<form method='post' action='/rebuild' data-busy='Rebuilding the databases...'>"
            + _button("Rebuild the local databases")
            + "</form>"
            "<p class='note'>This deletes the local databases and rebuilds them from the health "
            "data already downloaded. Nothing is re-downloaded from Garmin, and no health data is "
            "lost, but it can take several minutes.</p>"
        )

    if status.state is LinkState.AWAITING_MFA:
        body.append(
            "<form method='post' action='/setup/mfa' data-busy='Verifying your code with Garmin...'>"
            "<label>Verification code<input name='code' inputmode='numeric' autocomplete='one-time-code'"
            " required autofocus></label>" + _button("Finish linking") + "</form>"
            "<p class='note'>The challenge is held in memory and does not survive a restart or a "
            "rejected code. If either happens, sign in again.</p>"
        )
    elif status.state is LinkState.LINKED:
        body.append(
            f"<p class='sync'>{sync_summary}</p>"
            "<form method='post' action='/sync' data-busy='Starting a sync...'>"
            + _button("Sync now")
            + "</form>"
            "<p class='note'>A first sync backfills years of data and can run for tens of minutes. "
            "It continues in the background; this page does not need to stay open.</p>"
            "<form method='post' action='/setup/unlink'>"
            + _button("Unlink this Garmin account")
            + "</form>"
            "<p class='note'>Unlinking deletes the saved token. Your downloaded health data is kept.</p>"
        )
    else:
        body.append(
            "<form method='post' action='/setup/credentials' data-busy='Contacting Garmin...'>"
            f"<label>Garmin Connect email<input name='email' type='email' value='{email}' required></label>"
            "<label>Password<input name='password' type='password' autocomplete='current-password'"
            " required></label>" + _button("Link Garmin account") + "</form>"
            "<p class='note'>Signing in to Garmin takes a few seconds, and longer if your account "
            "uses MFA. Leave this page open while it works.</p>"
        )

    # aria-live so a screen reader announces the in-flight message when it appears.
    body.append("<p class='busy' id='busy' role='status' aria-live='polite' hidden></p>")
    body.append(f"<p class='warning'>{escape(PASSWORD_NOTICE)}</p>")
    body.append(BUSY_SCRIPT)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Garmin Connect setup</title><style>"
        + STYLE
        + "</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )


@get("/setup", media_type=MediaType.HTML, sync_to_thread=False)
def setup_page(state: State) -> str:
    authenticator = _authenticator(state)
    # take_error(), not peek: rendering the failure consumes it, so refreshing the
    # page does not keep reporting a sign-in that failed once.
    return _render(
        authenticator.status(),
        authenticator.take_error(),
        _describe_last_sync(_sync_engine(state)),
        _serving_fault(state),
    )


@get("/setup/status", sync_to_thread=False)
def setup_status(state: State) -> dict[str, str | None]:
    authenticator = _authenticator(state)
    # peek, not take: this endpoint is pollable and must not steal the flash that
    # /setup still has to render.
    return {**authenticator.status().as_dict(), "error": authenticator.peek_error()}


@post("/setup/credentials", status_code=HTTP_303_SEE_OTHER)
async def submit_credentials(
    state: State,
    data: Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    # AuthError is deliberately swallowed here: its message is held as a one-shot
    # flash and rendered once on /setup, so a failed attempt is an ordinary page
    # with an explanation rather than a 500.
    try:
        await _authenticator(state).login(data.get("email", ""), data.get("password", ""))
    except AuthError:
        pass
    return Redirect("/setup", status_code=HTTP_303_SEE_OTHER)


@post("/setup/mfa", status_code=HTTP_303_SEE_OTHER)
async def submit_mfa(
    state: State,
    data: Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    try:
        await _authenticator(state).complete_mfa(data.get("code", ""))
    except AuthError:
        pass
    return Redirect("/setup", status_code=HTTP_303_SEE_OTHER)


@post("/setup/unlink", status_code=HTTP_303_SEE_OTHER)
async def unlink(state: State) -> Redirect:
    await _authenticator(state).unlink()
    return Redirect("/setup", status_code=HTTP_303_SEE_OTHER)


@post("/sync", status_code=HTTP_202_ACCEPTED)
async def trigger_sync(state: State) -> dict[str, object]:
    """Kick off a sync now. Returns immediately; the work runs in the background."""
    authenticator = _authenticator(state)
    if authenticator.status().state is not LinkState.LINKED:
        # Silently doing nothing would look like a working sync that never
        # produces data, which is far harder to diagnose than a refusal.
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="Cannot sync until the Garmin account is linked. Visit /setup.",
        )
    started = await _sync_engine(state).trigger()
    return {
        "started": started,
        "detail": "Sync started." if started else "A sync is already running.",
    }


@post("/rebuild", status_code=HTTP_202_ACCEPTED)
async def trigger_rebuild(state: State) -> dict[str, object]:
    """Delete and reimport the local databases. Returns immediately.

    Deliberately not gated on the account being linked: a rebuild touches nothing
    but local files, and refusing it while unlinked would strand a container whose
    corpus is broken and whose token has since expired.
    """
    started = await _sync_engine(state).trigger_rebuild()
    return {
        "started": started,
        "detail": "Rebuild started." if started else "A sync or rebuild is already running.",
    }


@get("/sync/status", sync_to_thread=True)
def sync_status(state: State) -> dict[str, object]:
    """Sync state plus the serving layer's own health.

    The two fail independently: a corpus can be perfectly fresh and still be
    unservable because its schema needs rebuilding, and that is a fault only the
    owner can clear -- so it has to be visible somewhere the owner looks.
    """
    status: dict[str, object] = _sync_engine(state).status()
    service = state.get("health_service")
    status["serving"] = service.status() if service is not None else {"available": False}
    return status


owner_router = Router(
    path="/",
    route_handlers=[
        setup_page,
        setup_status,
        submit_credentials,
        submit_mfa,
        unlink,
        trigger_sync,
        trigger_rebuild,
        sync_status,
    ],
    guards=[owner_guard],
)
