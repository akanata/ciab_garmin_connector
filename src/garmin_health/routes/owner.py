"""Owner-facing setup surface: /setup and its form posts.

The router strips any client-supplied ``X-OpenHost-*`` header before stamping its
own, so ``X-OpenHost-Is-Owner`` is trustworthy. These paths are also kept out of
the manifest's ``public_paths``, so this guard is the second of two locks.

The only HTML in the project is here, and it is plain server-rendered markup: this
is a JSON API with one setup page, not a frontend.
"""

from __future__ import annotations

import datetime as dt
from html import escape
from typing import Annotated
from typing import Any

import attrs
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
from litestar.response import Response
from litestar.status_codes import HTTP_202_ACCEPTED
from litestar.status_codes import HTTP_303_SEE_OTHER
from litestar.status_codes import HTTP_409_CONFLICT

from garmin_health.auth import AuthError
from garmin_health.auth import AuthStatus
from garmin_health.auth import GarminAuthenticator
from garmin_health.auth import LinkState
from garmin_health.garmin_config import ensure_config
from garmin_health.preferences import DOWNLOADABLE_STATS
from garmin_health.preferences import STAT_DETAIL
from garmin_health.preferences import STAT_LABELS
from garmin_health.preferences import SYNC_INTERVAL_CHOICES
from garmin_health.preferences import ImportPreferences
from garmin_health.preferences import InvalidPreferences
from garmin_health.preferences import load_preferences
from garmin_health.preferences import parse_preferences
from garmin_health.preferences import save_preferences
from garmin_health.preferences import sync_interval_label
from garmin_health.sync import StatCoverage
from garmin_health.sync import SyncEngine
from garmin_health.sync import SyncStep

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


def _describe_schedule(engine: SyncEngine) -> str:
    """When the next automatic sync runs, so nobody has to wonder whether to press Sync."""
    status = engine.status()
    every = sync_interval_label(int(status["interval_seconds"])).lower()
    next_at = status["next_sync_at"]
    if next_at is None:
        return escape(f"Syncs automatically {every}; the first automatic sync starts shortly.")
    remaining = (dt.datetime.fromisoformat(next_at) - dt.datetime.now(dt.UTC)).total_seconds()
    if remaining <= 60:
        when = "shortly"
    elif remaining < 60 * 60:
        minutes = round(remaining / 60)
        when = f"in about {minutes} minute{'' if minutes == 1 else 's'}"
    else:
        hours = round(remaining / (60 * 60))
        when = f"in about {hours} hour{'' if hours == 1 else 's'}"
    return escape(f"Next automatic sync {when} (syncs {every}).")


@attrs.frozen
class PageView:
    """Everything /setup renders, assembled by the handler and passed down whole.

    A single value object rather than eight positional arguments: the page grew
    three independent concerns (linking, import scope, corpus coverage) and a
    render signature that long is one transposed argument away from a wrong page.
    """

    status: AuthStatus
    sync_summary: str
    preferences: ImportPreferences
    coverage: list[StatCoverage] = attrs.field(factory=list)
    step: SyncStep | None = None
    error: str | None = None
    scope_error: str | None = None
    serving_fault: str | None = None
    today: dt.date = attrs.field(factory=dt.date.today)

    @property
    def gapped(self) -> list[StatCoverage]:
        """Enabled metrics holding less history than the owner asked for."""
        return [c for c in self.coverage if c.enabled and c.has_gap]


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
    # Poll while a run is in flight so the step line stays current, then reload
    # once, so the coverage table and summary are rebuilt server-side rather than
    # duplicated in JavaScript. Pure enhancement: without it the page still shows
    # the step it was rendered with, and a refresh still updates everything.
    "(function(){var el=document.getElementById('step');if(!el)return;var ran=el.hidden?false:true;"
    "function tick(){fetch('/sync/status',{headers:{'accept':'application/json'}})"
    ".then(function(r){return r.ok?r.json():null}).then(function(s){if(!s)return;"
    "if(s.progress){ran=true;el.hidden=false;"
    "el.textContent=s.progress.label+(s.progress.total?' ('+s.progress.done+' of '+s.progress.total+')':'');}"
    "else if(ran){location.reload();return;}else{el.hidden=true;}"
    "setTimeout(tick,3000);}).catch(function(){setTimeout(tick,10000);});}"
    "setTimeout(tick,3000);})();"
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
    "select{display:block;width:100%;padding:.5rem;margin-top:.25rem}"
    "textarea{display:block;width:100%;padding:.5rem;margin-top:.25rem;font-family:ui-monospace,"
    "monospace;font-size:.8rem}"
    "pre{background:#f1f5f9;padding:.75rem;overflow-x:auto;font-size:.8rem;border-radius:3px}"
    "code{background:#f1f5f9;padding:.1em .3em;border-radius:3px;font-size:.9em}"
    "[hidden]{display:none!important}"
    ".error{padding:.75rem;background:#fdecea;border-left:3px solid #d32f2f}"
    ".detail{padding:.75rem;background:#fff4e5;border-left:3px solid #d97706}"
    ".warning,.note{color:#555;font-size:.875rem}"
    ".busy{color:#555;font-size:.875rem;margin-top:.5rem}"
    ".sync{padding:.75rem;background:#eef6ff;border-left:3px solid #2563eb}"
    ".step{padding:.75rem;background:#f1f5f9;border-left:3px solid #64748b;font-size:.9rem}"
    "h2{font-size:1.1rem;margin-top:2rem;border-top:1px solid #e2e8f0;padding-top:1.25rem}"
    "fieldset{border:1px solid #e2e8f0;margin:.75rem 0;padding:.5rem .75rem}"
    "legend{font-size:.875rem;color:#555;padding:0 .25rem}"
    "label.check{display:flex;gap:.5rem;align-items:flex-start;margin:.6rem 0}"
    "label.check input{width:auto;margin:.2rem 0 0}"
    "label.check .note{display:block;margin-top:.1rem}"
    "table.coverage{border-collapse:collapse;width:100%;font-size:.9rem;margin-top:.5rem}"
    "table.coverage th,table.coverage td{text-align:left;padding:.4rem .5rem;"
    "border-bottom:1px solid #e2e8f0;vertical-align:top}"
    "table.coverage thead th{font-size:.8rem;color:#555;font-weight:600}"
    "td.empty{color:#777}td.gap{color:#b45309}td.ok{color:#15803d}"
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


def _interval_options(current: int) -> list[int]:
    """The offered intervals, plus the saved one if the operator set something else."""
    return sorted(set(SYNC_INTERVAL_CHOICES) | {current})


def _import_scope_form(view: PageView) -> str:
    """The three knobs that decide how much work importing does."""
    prefs = view.preferences
    boxes = []
    for stat in DOWNLOADABLE_STATS:
        checked = " checked" if prefs.is_enabled(stat) else ""
        boxes.append(
            f"<label class='check'><input type='checkbox' name='stats' value='{stat}'{checked}> "
            f"{escape(STAT_LABELS[stat])}"
            f"<span class='note'>{escape(STAT_DETAIL[stat])}</span></label>"
        )

    options = "".join(
        f"<option value='{seconds}'{' selected' if seconds == prefs.sync_interval_seconds else ''}>"
        f"{escape(sync_interval_label(seconds))}</option>"
        for seconds in _interval_options(prefs.sync_interval_seconds)
    )

    warning = ""
    if not prefs.enabled_stats:
        warning = (
            "<p class='detail'>Imports are paused: no metrics are selected, so a sync will "
            "download nothing.</p>"
        )

    error = (
        f"<p class='error' role='alert'>{escape(view.scope_error)}</p>" if view.scope_error else ""
    )
    return (
        "<h2>Import settings</h2>"
        "<p class='note'>The start date and metrics decide how long each sync takes; the "
        "interval decides how often one runs. A full history of continuous heart rate is by far "
        "the slowest thing to fetch.</p>"
        + error
        + "<form method='post' action='/setup/import' data-busy='Saving...'>"
        "<label>Earliest date to import from"
        f"<input type='date' name='start_date' value='{escape(prefs.start_date_text, quote=True)}'"
        f" max='{view.today.isoformat()}' required></label>"
        "<label>Check Garmin for new data"
        f"<select name='sync_interval'>{options}</select>"
        "<span class='note'>Your watch only reaches Garmin Connect when it syncs with your "
        "phone, so checking more often than that fetches nothing new.</span></label>"
        "<fieldset><legend>Metrics to import</legend>"
        + "".join(boxes)
        + "</fieldset>"
        + _button("Save import settings")
        + "</form>"
        + warning
    )


MINT_SNIPPET = """from garminconnect import Garmin
g = Garmin("you@example.com", "your-password")
g.login()                              # answer the MFA prompt if you have one
g.client.dump("./garmin_tokens.json")"""


def _token_import_form() -> str:
    """The way past a Cloudflare bot challenge on Garmin's sign-in portal.

    The challenge guards only the interactive credential exchange. Refresh and
    data use different hosts, so a token minted anywhere links this app for the
    life of its refresh token.
    """
    return (
        "<h2>Sign-in blocked by a bot challenge?</h2>"
        "<p class='note'>Garmin's sign-in page sits behind a Cloudflare bot check that a server "
        "often cannot pass, even though your account and password are fine. You can sign in once "
        "on a computer that <em>can</em> reach it and bring the resulting token here &mdash; this "
        "app only ever needs the token, and refreshing it later does not go through the blocked "
        "page.</p>"
        "<p class='note'>On that computer, with <code>pip install garminconnect</code>:</p>"
        f"<pre>{escape(MINT_SNIPPET)}</pre>"
        "<p class='note'>Then paste the contents of the <code>garmin_tokens.json</code> it "
        "writes:</p>"
        "<form method='post' action='/setup/token' data-busy='Checking the token with Garmin...'>"
        "<label>Garmin Connect email <span class='note'>optional, only labels this page</span>"
        "<input name='email' type='email' autocomplete='off'></label>"
        "<label>garmin_tokens.json"
        "<textarea name='token' rows='4' required autocomplete='off' spellcheck='false'"
        ' placeholder=\'{"di_token": ..., "di_refresh_token": ..., "di_client_id": ...}\'>'
        "</textarea></label>" + _button("Link with this token") + "</form>"
        "<p class='warning'>That file grants ongoing access to your Garmin account, exactly as a "
        "password would. It is stored owner-only and never shown again.</p>"
    )


def _join_names(names: list[str]) -> str:
    """ "a", "a and b", "a, b and c" -- this is prose the owner reads."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _coverage_table(view: PageView) -> str:
    """What each metric actually holds, and whether it reaches the chosen floor."""
    if not view.coverage:
        return ""

    rows = []
    for entry in view.coverage:
        if entry.rows == 0:
            held = "<td class='empty'>nothing yet</td><td class='empty'>&mdash;</td>"
        else:
            held = (
                f"<td>{entry.rows:,} rows</td>"
                f"<td>{escape(str(entry.earliest))} &rarr; {escape(str(entry.latest))}</td>"
            )
        if not entry.enabled:
            # It may well hold data from before it was switched off, so "paused"
            # rather than "not imported" -- the Held column already says what is
            # there, and this column is about what happens next.
            note = "<td class='empty'>paused</td>"
        elif entry.rows == 0:
            # has_gap is deliberately False here (a normal sync already starts an
            # empty metric at the floor), but "complete" would be a plain lie.
            note = "<td class='empty'>starts at your date on the next sync</td>"
        elif entry.has_gap:
            note = f"<td class='gap'>{entry.missing_days:,} older days missing</td>"
        else:
            note = "<td class='ok'>complete</td>"
        rows.append(f"<tr><th scope='row'>{escape(STAT_LABELS[entry.stat])}</th>{held}{note}</tr>")

    backfill = ""
    if view.gapped:
        worst = max(c.missing_days for c in view.gapped)
        names = _join_names([STAT_LABELS[c.stat] for c in view.gapped])
        backfill = (
            f"<p class='detail'>{escape(names)} hold less history than you asked for &mdash; up "
            f"to {worst:,} days. A normal sync only ever moves forward from the newest reading, "
            "so older history has to be fetched deliberately.</p>"
            "<form method='post' action='/backfill' data-busy='Starting the backfill...'>"
            + _button("Download the missing older history")
            + "</form>"
            "<p class='note'>This fetches only the missing older range, roughly a second per day "
            "per metric. It runs in the background; this page does not need to stay open.</p>"
        )

    return (
        "<h2>What has been imported</h2>"
        "<table class='coverage'><thead><tr><th scope='col'>Metric</th>"
        "<th scope='col'>Held</th><th scope='col'>Covering</th>"
        "<th scope='col'>Against your start date</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>" + backfill
    )


def _progress_line(step: SyncStep | None) -> str:
    """The live step, rendered server-side so it is there without JavaScript."""
    if step is None:
        return "<p class='step' id='step' hidden></p>"
    counter = f" ({step.done} of {step.total})" if step.total else ""
    return f"<p class='step' id='step'>{escape(step.label)}{escape(counter)}</p>"


def _render(view: PageView) -> str:
    status = view.status
    error = view.error
    sync_summary = view.sync_summary
    serving_fault = view.serving_fault
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
            + _progress_line(view.step)
            + "<form method='post' action='/sync' data-busy='Starting a sync...'>"
            + _button("Sync now")
            + "</form>"
            "<p class='note'>A first sync backfills years of data and can run for tens of minutes. "
            "It continues in the background; this page does not need to stay open.</p>"
            + _import_scope_form(view)
            + _coverage_table(view)
            + "<form method='post' action='/setup/unlink'>"
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
            "uses MFA. Leave this page open while it works.</p>" + _token_import_form()
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


async def _page_view(
    state: State, *, scope_error: str | None = None, consume_flash: bool = True
) -> PageView:
    authenticator = _authenticator(state)
    engine = _sync_engine(state)
    status = authenticator.status()
    coverage: list[StatCoverage] = []
    if status.state is LinkState.LINKED:
        # Only worth the query once there is an account to have downloaded
        # anything; behind a TTL inside the engine either way.
        coverage = list((await engine.coverage()).values())
    return PageView(
        status=status,
        # take_error(), not peek: rendering the failure consumes it, so refreshing
        # the page does not keep reporting a sign-in that failed once.
        error=authenticator.take_error() if consume_flash else authenticator.peek_error(),
        sync_summary=f"{_describe_last_sync(engine)} {_describe_schedule(engine)}",
        preferences=load_preferences(state.settings),
        coverage=coverage,
        step=engine.step,
        scope_error=scope_error,
        serving_fault=_serving_fault(state),
    )


@get(["/", "/setup"], media_type=MediaType.HTML)
async def setup_page(state: State) -> str:
    return _render(await _page_view(state))


@post("/setup/import", media_type=MediaType.HTML)
async def submit_import_scope(
    state: State,
    data: Annotated[dict[str, Any], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response[str]:
    """Save how far back to import and which metrics to fetch.

    On success this redirects (post/redirect/get, so a refresh does not resubmit).
    On a validation failure it re-renders the page with the reason instead of
    redirecting, which keeps the explanation attached to the form that caused it
    without needing a flash that could outlive the mistake.
    """
    raw_stats = data.get("stats", [])
    # A single ticked checkbox arrives as a bare string, several as a list.
    stats = [raw_stats] if isinstance(raw_stats, str) else list(raw_stats)
    try:
        raw_interval = data.get("sync_interval")
        preferences = parse_preferences(
            state.settings,
            start_date=str(data.get("start_date", "")),
            stats=stats,
            sync_interval=None if raw_interval is None else str(raw_interval),
        )
    except InvalidPreferences as exc:
        view = await _page_view(state, scope_error=str(exc), consume_flash=False)
        return Response(content=_render(view), media_type=MediaType.HTML, status_code=400)

    save_preferences(state.settings, preferences)
    # Wake the loop, so a shorter interval applies now rather than after the long
    # wait it may already be part-way through.
    _sync_engine(state).reschedule()
    # Rewrite GarminConnectConfig.json now rather than at the next sync, so the
    # saved scope is what the next download reads even if this process restarts.
    ensure_config(state.settings, preferences=preferences)
    # The coverage table is measured against the floor that just changed.
    await _sync_engine(state).coverage(refresh=True)
    return Response(content="", status_code=HTTP_303_SEE_OTHER, headers={"Location": "/setup"})


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


@post("/setup/token", status_code=HTTP_303_SEE_OTHER)
async def submit_token(
    state: State,
    data: Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    """Link from a token minted elsewhere, for accounts the bot check blocks.

    Redirects either way, exactly like the credential form: the failure is held as
    a one-shot flash and rendered once. That also keeps the pasted token out of a
    re-rendered form field, and so out of browser history and screenshots.
    """
    try:
        await _authenticator(state).link_with_token(
            data.get("token", ""), email=data.get("email", "")
        )
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


@post("/backfill", status_code=HTTP_202_ACCEPTED)
async def trigger_backfill(state: State) -> dict[str, object]:
    """Fetch the older history each enabled metric is missing. Returns immediately.

    Needs a linked account, unlike a rebuild: this one really does talk to Garmin.
    """
    if _authenticator(state).status().state is not LinkState.LINKED:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="Cannot download history until the Garmin account is linked. Visit /setup.",
        )
    started = await _sync_engine(state).trigger(backfill=True)
    return {
        "started": started,
        "detail": "Backfill started." if started else "A sync is already running.",
    }


@get("/sync/status")
async def sync_status(state: State) -> dict[str, object]:
    """Sync state plus the serving layer's own health.

    The two fail independently: a corpus can be perfectly fresh and still be
    unservable because its schema needs rebuilding, and that is a fault only the
    owner can clear -- so it has to be visible somewhere the owner looks.
    """
    engine = _sync_engine(state)
    status: dict[str, object] = engine.status()
    service = state.get("health_service")
    status["serving"] = service.status() if service is not None else {"available": False}
    status["coverage"] = [c.as_dict() for c in (await engine.coverage()).values()]
    return status


owner_router = Router(
    path="/",
    route_handlers=[
        setup_page,
        setup_status,
        submit_credentials,
        submit_mfa,
        submit_token,
        unlink,
        submit_import_scope,
        trigger_sync,
        trigger_backfill,
        trigger_rebuild,
        sync_status,
    ],
    guards=[owner_guard],
)
