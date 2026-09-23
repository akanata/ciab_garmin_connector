"""GarminDB's half of the owner page, and the form posts behind it.

Everything here exists because the data is acquired by driving GarminDB: a real
Garmin password replayed against an SSO form, an MFA challenge, a token minted
elsewhere to get past a bot challenge, an import scope measured in downloaded
days, and a rebuild that only means anything to a local SQLite corpus. An
aggregator would replace all of it with a redirect and a webhook, which is why
none of it belongs in the shell.

The shell is in ``garmin_health.setup_page`` and is imported, never the other way
round -- and this module imports nothing from ``garmin_health.routes``, so the
provider stays a leaf.
"""

from __future__ import annotations

import datetime as dt
from html import escape
from typing import TYPE_CHECKING
from typing import Annotated
from typing import Any

import attrs
from litestar import post
from litestar.datastructures import State
from litestar.enums import MediaType
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.params import Body
from litestar.response import Redirect
from litestar.response import Response
from litestar.status_codes import HTTP_202_ACCEPTED
from litestar.status_codes import HTTP_303_SEE_OTHER
from litestar.status_codes import HTTP_409_CONFLICT

from garmin_health.ports import LinkState
from garmin_health.ports import SetupView
from garmin_health.progress import SyncStep
from garmin_health.providers.garmindb.auth import AuthError
from garmin_health.providers.garmindb.config_file import ensure_config
from garmin_health.providers.garmindb.preferences import DOWNLOADABLE_STATS
from garmin_health.providers.garmindb.preferences import STAT_DETAIL
from garmin_health.providers.garmindb.preferences import STAT_LABELS
from garmin_health.providers.garmindb.preferences import SYNC_INTERVAL_CHOICES
from garmin_health.providers.garmindb.preferences import ImportPreferences
from garmin_health.providers.garmindb.preferences import InvalidPreferences
from garmin_health.providers.garmindb.preferences import load_preferences
from garmin_health.providers.garmindb.preferences import parse_preferences
from garmin_health.providers.garmindb.preferences import save_preferences
from garmin_health.providers.garmindb.preferences import sync_interval_label
from garmin_health.providers.garmindb.sync import StatCoverage
from garmin_health.providers.garmindb.sync import SyncEngine
from garmin_health.setup_page import button
from garmin_health.setup_page import progress_line
from garmin_health.setup_page import render_setup_page

if TYPE_CHECKING:
    # Type-only: at runtime ``provider.py`` imports the handlers below, and
    # importing it back here would be a cycle.
    from garmin_health.providers.garmindb.provider import GarminDbProvider

PASSWORD_NOTICE = (
    "Garmin does not offer a consent-based API outside its developer portal, so linking "
    "replays your real Garmin Connect password against Garmin's sign-in form once. It is "
    "used for that request only, is never written to disk, and is discarded as soon as a "
    "token is saved. Only you, the owner of this bottle, can reach this page."
)

MINT_SNIPPET = """from garminconnect import Garmin
g = Garmin("you@example.com", "your-password")
g.login()                              # answer the MFA prompt if you have one
g.client.dump("./garmin_tokens.json")"""


def _provider(state: State) -> GarminDbProvider:
    provider: GarminDbProvider = state.provider
    return provider


# -- page fragments -----------------------------------------------------------


@attrs.frozen
class PageData:
    """The shell's view plus everything only this provider can answer for.

    A single value object rather than eight positional arguments: this half of
    the page grew three independent concerns (linking, import scope, corpus
    coverage) and a render signature that long is one transposed argument away
    from a wrong page.
    """

    view: SetupView
    sync_summary: str
    preferences: ImportPreferences
    coverage: list[StatCoverage] = attrs.field(factory=list)
    step: SyncStep | None = None

    @property
    def gapped(self) -> list[StatCoverage]:
        """Enabled metrics holding less history than the owner asked for."""
        return [c for c in self.coverage if c.enabled and c.has_gap]


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


def _interval_options(current: int) -> list[int]:
    """The offered intervals, plus the saved one if the operator set something else."""
    return sorted(set(SYNC_INTERVAL_CHOICES) | {current})


def _import_scope_form(data: PageData) -> str:
    """The three knobs that decide how much work importing does."""
    prefs = data.preferences
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

    scope_error = data.view.provider_error
    error = f"<p class='error' role='alert'>{escape(scope_error)}</p>" if scope_error else ""
    return (
        "<h2>Import settings</h2>"
        "<p class='note'>The start date and metrics decide how long each sync takes; the "
        "interval decides how often one runs. A full history of continuous heart rate is by far "
        "the slowest thing to fetch.</p>"
        + error
        + "<form method='post' action='/setup/import' data-busy='Saving...'>"
        "<label>Earliest date to import from"
        f"<input type='date' name='start_date' value='{escape(prefs.start_date_text, quote=True)}'"
        f" max='{data.view.today.isoformat()}' required></label>"
        "<label>Check Garmin for new data"
        f"<select name='sync_interval'>{options}</select>"
        "<span class='note'>Your watch only reaches Garmin Connect when it syncs with your "
        "phone, so checking more often than that fetches nothing new.</span></label>"
        "<fieldset><legend>Metrics to import</legend>"
        + "".join(boxes)
        + "</fieldset>"
        + button("Save import settings")
        + "</form>"
        + warning
    )


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
        "</textarea></label>" + button("Link with this token") + "</form>"
        "<p class='warning'>That file grants ongoing access to your Garmin account, exactly as a "
        "password would. It is stored owner-only and never shown again.</p>"
    )


def _join_names(names: list[str]) -> str:
    """ "a", "a and b", "a, b and c" -- this is prose the owner reads."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _coverage_table(data: PageData) -> str:
    """What each metric actually holds, and whether it reaches the chosen floor."""
    if not data.coverage:
        return ""

    rows = []
    for entry in data.coverage:
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
    if data.gapped:
        worst = max(c.missing_days for c in data.gapped)
        names = _join_names([STAT_LABELS[c.stat] for c in data.gapped])
        backfill = (
            f"<p class='detail'>{escape(names)} hold less history than you asked for &mdash; up "
            f"to {worst:,} days. A normal sync only ever moves forward from the newest reading, "
            "so older history has to be fetched deliberately.</p>"
            "<form method='post' action='/backfill' data-busy='Starting the backfill...'>"
            + button("Download the missing older history")
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


def _rebuild_section(fault: str) -> str:
    """Offered only while serving is actually degraded.

    A destructive action should not sit on the page when nothing is wrong, and
    deleting the databases is the owner's only way out of a schema mismatch in a
    container.
    """
    return (
        f"<p class='detail'>{escape(fault)}</p>"
        "<form method='post' action='/rebuild' data-busy='Rebuilding the databases...'>"
        + button("Rebuild the local databases")
        + "</form>"
        "<p class='note'>This deletes the local databases and rebuilds them from the health "
        "data already downloaded. Nothing is re-downloaded from Garmin, and no health data is "
        "lost, but it can take several minutes.</p>"
    )


def _mfa_form() -> str:
    return (
        "<form method='post' action='/setup/mfa' data-busy='Verifying your code with Garmin...'>"
        "<label>Verification code<input name='code' inputmode='numeric' autocomplete='one-time-code'"
        " required autofocus></label>" + button("Finish linking") + "</form>"
        "<p class='note'>The challenge is held in memory and does not survive a restart or a "
        "rejected code. If either happens, sign in again.</p>"
    )


def _linked_section(data: PageData) -> str:
    return (
        f"<p class='sync'>{data.sync_summary}</p>"
        + progress_line(data.step)
        + "<form method='post' action='/sync' data-busy='Starting a sync...'>"
        + button("Sync now")
        + "</form>"
        "<p class='note'>A first sync backfills years of data and can run for tens of minutes. "
        "It continues in the background; this page does not need to stay open.</p>"
        + _import_scope_form(data)
        + _coverage_table(data)
        + "<form method='post' action='/setup/unlink'>"
        + button("Unlink this Garmin account")
        + "</form>"
        "<p class='note'>Unlinking deletes the saved token. Your downloaded health data is kept.</p>"
    )


def _credentials_section(account: str | None) -> str:
    email = escape(account or "", quote=True)
    return (
        "<form method='post' action='/setup/credentials' data-busy='Contacting Garmin...'>"
        f"<label>Garmin Connect email<input name='email' type='email' value='{email}' required></label>"
        "<label>Password<input name='password' type='password' autocomplete='current-password'"
        " required></label>" + button("Link Garmin account") + "</form>"
        "<p class='note'>Signing in to Garmin takes a few seconds, and longer if your account "
        "uses MFA. Leave this page open while it works.</p>" + _token_import_form()
    )


async def render_fragment(provider: GarminDbProvider, view: SetupView) -> str:
    """This provider's half of ``/setup``, assembled for the shell."""
    engine = provider.engine
    coverage: list[StatCoverage] = []
    if view.link.state is LinkState.LINKED:
        # Only worth the query once there is an account to have downloaded
        # anything; behind a TTL inside the engine either way.
        coverage = list((await engine.coverage()).values())
    data = PageData(
        view=view,
        sync_summary=f"{_describe_last_sync(engine)} {_describe_schedule(engine)}",
        preferences=load_preferences(provider.settings),
        coverage=coverage,
        step=engine.step,
    )

    parts = []
    if view.serving_fault:
        parts.append(_rebuild_section(view.serving_fault))
    if view.link.state is LinkState.PENDING:
        parts.append(_mfa_form())
    elif view.link.state is LinkState.LINKED:
        parts.append(_linked_section(data))
    else:
        parts.append(_credentials_section(view.link.account))
    parts.append(f"<p class='warning'>{escape(PASSWORD_NOTICE)}</p>")
    return "".join(parts)


# -- handlers -----------------------------------------------------------------


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
    provider = _provider(state)
    settings = provider.settings
    raw_stats = data.get("stats", [])
    # A single ticked checkbox arrives as a bare string, several as a list.
    stats = [raw_stats] if isinstance(raw_stats, str) else list(raw_stats)
    try:
        raw_interval = data.get("sync_interval")
        preferences = parse_preferences(
            settings,
            start_date=str(data.get("start_date", "")),
            stats=stats,
            sync_interval=None if raw_interval is None else str(raw_interval),
        )
    except InvalidPreferences as exc:
        # consume_flash=False: this re-render must not eat an unrelated sign-in
        # flash that /setup still has to show.
        page = await render_setup_page(state, consume_flash=False, provider_error=str(exc))
        return Response(content=page, media_type=MediaType.HTML, status_code=400)

    save_preferences(settings, preferences)
    # Wake the loop, so a shorter interval applies now rather than after the long
    # wait it may already be part-way through.
    provider.engine.reschedule()
    # Rewrite GarminConnectConfig.json now rather than at the next sync, so the
    # saved scope is what the next download reads even if this process restarts.
    ensure_config(settings, preferences=preferences)
    # The coverage table is measured against the floor that just changed.
    await provider.engine.coverage(refresh=True)
    return Response(content="", status_code=HTTP_303_SEE_OTHER, headers={"Location": "/setup"})


@post("/setup/credentials", status_code=HTTP_303_SEE_OTHER)
async def submit_credentials(
    state: State,
    data: Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    # AuthError is deliberately swallowed here: its message is held as a one-shot
    # flash and rendered once on /setup, so a failed attempt is an ordinary page
    # with an explanation rather than a 500.
    try:
        await _provider(state).authenticator.login(data.get("email", ""), data.get("password", ""))
    except AuthError:
        pass
    return Redirect("/setup", status_code=HTTP_303_SEE_OTHER)


@post("/setup/mfa", status_code=HTTP_303_SEE_OTHER)
async def submit_mfa(
    state: State,
    data: Annotated[dict[str, str], Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Redirect:
    try:
        await _provider(state).authenticator.complete_mfa(data.get("code", ""))
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
        await _provider(state).authenticator.link_with_token(
            data.get("token", ""), email=data.get("email", "")
        )
    except AuthError:
        pass
    return Redirect("/setup", status_code=HTTP_303_SEE_OTHER)


@post("/sync", status_code=HTTP_202_ACCEPTED)
async def trigger_sync(state: State) -> dict[str, object]:
    """Kick off a sync now. Returns immediately; the work runs in the background."""
    provider = _provider(state)
    if provider.link.status().state is not LinkState.LINKED:
        # Silently doing nothing would look like a working sync that never
        # produces data, which is far harder to diagnose than a refusal.
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="Cannot sync until the Garmin account is linked. Visit /setup.",
        )
    started = await provider.engine.trigger()
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
    started = await _provider(state).engine.trigger_rebuild()
    return {
        "started": started,
        "detail": "Rebuild started." if started else "A sync or rebuild is already running.",
    }


@post("/backfill", status_code=HTTP_202_ACCEPTED)
async def trigger_backfill(state: State) -> dict[str, object]:
    """Fetch the older history each enabled metric is missing. Returns immediately.

    Needs a linked account, unlike a rebuild: this one really does talk to Garmin.
    """
    provider = _provider(state)
    if provider.link.status().state is not LinkState.LINKED:
        raise HTTPException(
            status_code=HTTP_409_CONFLICT,
            detail="Cannot download history until the Garmin account is linked. Visit /setup.",
        )
    started = await provider.engine.trigger(backfill=True)
    return {
        "started": started,
        "detail": "Backfill started." if started else "A sync is already running.",
    }


OWNER_HANDLERS = [
    submit_import_scope,
    submit_credentials,
    submit_mfa,
    submit_token,
    trigger_sync,
    trigger_backfill,
    trigger_rebuild,
]
