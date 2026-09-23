"""The owner page's shell, and the markup primitives a provider builds with.

Generic on purpose, and deliberately **not** under ``routes/``: a provider
renders its own half of ``/setup`` and re-renders the whole page when one of its
forms fails validation, so it has to import this -- and nothing under
``providers/`` may import ``routes.*``.

The split is by ownership, not by markup. The shell owns the document, the
stylesheet, the polling script, the headline for each :class:`LinkState` and the
one-shot flash; the provider owns everything that depends on how data is actually
acquired. ``BUSY_SCRIPT`` stays here because it is written against the wire shape
of ``/sync/status`` (``progress.label``/``done``/``total``), which every provider
must report.

The only HTML in the project is here and in the providers' own page fragments,
and it is plain server-rendered markup: this is a JSON API with one setup page,
not a frontend.
"""

from __future__ import annotations

import datetime as dt
from html import escape

from litestar.datastructures import State

from garmin_health.ports import LinkState
from garmin_health.ports import Provider
from garmin_health.ports import SetupView
from garmin_health.progress import SyncStep

# Progressive enhancement only. The plain form POST is what submits; this just
# reports that a (multi-second, real) round-trip is under way. If a CSP blocks
# the inline script, linking still works with no feedback.
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


def button(label: str) -> str:
    return f"<button type='submit'><span class='spinner' aria-hidden='true'></span>{escape(label)}</button>"


def progress_line(step: SyncStep | None) -> str:
    """The live step, rendered server-side so it is there without JavaScript."""
    if step is None:
        return "<p class='step' id='step' hidden></p>"
    counter = f" ({step.done} of {step.total})" if step.total else ""
    return f"<p class='step' id='step'>{escape(step.label)}{escape(counter)}</p>"


def provider_of(state: State) -> Provider:
    provider: Provider = state.provider
    return provider


def serving_fault_of(state: State) -> str | None:
    """The serving layer's fault, if it has one.

    Acquisition health and serving health fail independently: a store can be
    perfectly fresh and still be unservable because its schema needs rebuilding.
    That is a fault only the owner can clear, so it has to appear where the owner
    looks.
    """
    service = state.get("health_service")
    if service is None:
        return None
    fault: str | None = service.fault
    return fault


def _headline(provider: Provider, link: LinkState, account: str | None) -> str:
    name = provider.display_name
    if link is LinkState.LINKED:
        return f"Linked to {name} as {account}." if account else f"Linked to {name}."
    if link is LinkState.PENDING:
        # No mention of MFA: an aggregator's pending link is an OAuth redirect
        # the owner has not come back from. The specific instruction is in
        # LinkStatus.detail, which the provider writes.
        return f"Finishing the link to {name}."
    if link is LinkState.NEEDS_REAUTH:
        return f"This bottle is no longer linked to {name}."
    return f"Not linked to {name} yet."


async def render_setup_page(
    state: State, *, consume_flash: bool = True, provider_error: str | None = None
) -> str:
    """Render ``/setup``: the shell, with the provider's fragment inside it.

    ``consume_flash`` is the difference between rendering the page and merely
    re-rendering it after a form failed. A 400 re-render must **not** eat the
    sign-in flash, or an unrelated failure would vanish unseen.
    """
    provider = provider_of(state)
    link = provider.link.status()
    view = SetupView(
        link=link,
        # take_error(), not peek: rendering the failure consumes it, so refreshing
        # the page does not keep reporting something that failed once.
        error=provider.link.take_error() if consume_flash else provider.link.peek_error(),
        serving_fault=serving_fault_of(state),
        provider_error=provider_error,
        today=dt.date.today(),
    )

    body = [
        f"<h1>{escape(provider.display_name)}</h1>",
        f"<p class='state' data-state='{link.state.value}'>"
        f"{escape(_headline(provider, link.state, link.account))}</p>",
    ]
    if view.error:
        body.append(f"<p class='error' role='alert'>{escape(view.error)}</p>")
    if link.detail:
        body.append(f"<p class='detail'>{escape(link.detail)}</p>")
    body.append(await provider.render_setup(view))
    # aria-live so a screen reader announces the in-flight message when it appears.
    body.append("<p class='busy' id='busy' role='status' aria-live='polite' hidden></p>")
    body.append(BUSY_SCRIPT)

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(provider.display_name)} setup</title><style>"
        + STYLE
        + "</style></head><body>"
        + "".join(body)
        + "</body></html>"
    )
