"""Settings read from the environment. No I/O.

What is here is the platform's business and every provider's: where the
persistent volume is, which provider to run, and the limits the serving layer
applies to any window.

Everything a *particular* provider needs -- a vendor domain, a timezone, a
download floor, a webhook secret -- belongs to that provider's own settings
object, which roots itself under ``app_data_dir``. See
``providers/garmindb/settings.py``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import attrs

DEFAULT_APP_DATA_DIR = Path("data")
DEFAULT_PROVIDER = "garmindb"

# Serving limits. A continuous heart-rate series is ~720 rows/day, so a year is
# ~263k rows and an unbounded request would build that many Sample objects.
#
# DEFAULT_LIMIT applies when the consumer sent no limit at all, so an unbounded
# request cannot exhaust memory. MAX_LIMIT is generous enough that a full day
# (720 rows) is never decimated in practice. MAX_ROWS_SCANNED bounds the *fetch*
# rather than the response, and is what turns a decade-wide window into a 413
# instead of a swap storm.
DEFAULT_LIMIT = 5_000
MAX_LIMIT = 50_000
MAX_ROWS_SCANNED = 1_000_000
# An 8-hour night is ~240 heart-rate and ~96 HRV rows, so this only ever bites on
# a corrupt window; it exists so one bad row cannot make a session unbounded.
MAX_SESSION_SUBSERIES = 2_000

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


class ConfigError(Exception):
    """The environment does not describe a runnable configuration."""


@attrs.frozen
class Settings:
    """What the app needs before it knows which provider it is running.

    ``app_data_dir`` is the router-provided persistent volume. Every provider
    roots its own files under it, so a container restart neither re-prompts for
    credentials nor re-downloads years of history.

    ``provider`` names which acquisition strategy to run. One app, one provider
    at a time, chosen by the operator rather than by the image.
    """

    app_data_dir: Path
    provider: str = DEFAULT_PROVIDER


def path_from(env: Mapping[str, str], *names: str) -> Path | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return Path(value)
    return None


def flag_from(env: Mapping[str, str], name: str) -> bool:
    """Parse a boolean knob. An unrecognised spelling is an error, not a False.

    Silently reading "maybe" as off would leave the owner believing they had
    switched something on.
    """
    raw = env.get(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {env[name]!r}")


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from ``env`` (defaults to ``os.environ``)."""
    env = os.environ if env is None else env

    # Cloud in a Bottle renamed OPENHOST_* to BOTTLE_*. Routers from 2026-08-27 on
    # export both; older ones export only the old name. Both are accepted with the new
    # one winning -- which is exactly why the image must never bake in a BOTTLE_ value.
    app_data_dir = (
        path_from(env, "BOTTLE_APP_DATA_DIR", "OPENHOST_APP_DATA_DIR") or DEFAULT_APP_DATA_DIR
    )
    provider = env.get("HEALTH_PROVIDER", "").strip() or DEFAULT_PROVIDER
    return Settings(app_data_dir=app_data_dir, provider=provider)
