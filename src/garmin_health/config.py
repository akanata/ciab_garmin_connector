"""Settings read from the environment. No I/O beyond validating a timezone name."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

import attrs
import dateutil.parser

DEFAULT_APP_DATA_DIR = Path("data")
# Freshness is capped by how often the watch syncs to Garmin Connect through the
# phone, so much shorter buys little; six hours left last night's sleep missing
# for most of a morning. The owner can change it on /setup.
DEFAULT_SYNC_INTERVAL_SECONDS = 60 * 60
# Matches GarminDB's own example. Downloads run ~1 second per day per stat, so a
# backfill from here is hours on first run; GARMIN_BACKFILL_START_DATE shortens it.
DEFAULT_BACKFILL_START_DATE = "2019-12-31"
SUPPORTED_DOMAINS = ("garmin.com", "garmin.cn")

# Serving limits. monitoring_hr is ~720 rows/day, so a year is ~263k rows and an
# unbounded request would build that many Sample objects.
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
    """Everything the app needs from its environment.

    ``app_data_dir`` is the router-provided persistent volume. Both the GarminDB
    config directory (which holds ``garmin_tokens.json``) and the downloaded
    HealthData corpus live under it, so a container restart neither re-prompts for
    MFA nor re-downloads years of FIT files.
    """

    app_data_dir: Path
    garmin_domain: str = "garmin.com"
    home_tz: str | None = None
    import_tz: str | None = None
    backfill_start_date: str = DEFAULT_BACKFILL_START_DATE
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS
    # Both default off because both would emit data Garmin never recorded. See
    # the sleep builder for what each one invents when switched on.
    fill_stage_gaps: bool = False
    derive_restless_periods: bool = False

    @property
    def config_dir(self) -> Path:
        """Holds GarminConnectConfig.json and garmin_tokens.json."""
        return self.app_data_dir / "GarminDb"

    @property
    def garmin_config_file(self) -> Path:
        return self.config_dir / "GarminConnectConfig.json"

    @property
    def token_file(self) -> Path:
        """Must match GarminConnectConfigManager.get_token_store_file()."""
        return self.config_dir / "garmin_tokens.json"

    @property
    def sync_state_file(self) -> Path:
        """When the last forward sync started.

        Has to survive a restart: without it every deploy waits out a whole
        interval before its first sync, and a crash-looping container would sign
        in to Garmin on every restart.
        """
        return self.app_data_dir / "sync_state.json"

    @property
    def preferences_file(self) -> Path:
        """Owner-editable import scope (how far back, which metrics).

        Under app data rather than in the GarminDb config directory because it is
        ours, not GarminDB's, and it has to survive a container restart -- a scope
        the owner re-enters after every deploy is not a setting.
        """
        return self.app_data_dir / "import_preferences.json"

    @property
    def health_data_dir(self) -> Path:
        """GarminDB's base_dir: the raw JSON/FIT corpus plus the SQLite DBs."""
        return self.app_data_dir / "HealthData"

    @property
    def db_dir(self) -> Path:
        """The GarminDB SQLite files.

        Exactly what ``GarminConnectConfigManager.get_db_dir()`` returns for our
        config. Derived directly so the serving side never has to render or read
        GarminConnectConfig.json, and therefore works on a container that has
        never linked an account.
        """
        return self.health_data_dir / "DBs"

    @property
    def is_cn(self) -> bool:
        return self.garmin_domain == "garmin.cn"


def _path_from(env: Mapping[str, str], *names: str) -> Path | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return Path(value)
    return None


def _sync_interval_from(env: Mapping[str, str]) -> int:
    raw = env.get("SYNC_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SYNC_INTERVAL_SECONDS
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ConfigError(f"SYNC_INTERVAL_SECONDS must be an integer, got {raw!r}") from exc
    if seconds <= 0:
        raise ConfigError(f"SYNC_INTERVAL_SECONDS must be positive, got {seconds}")
    return seconds


def _flag_from(env: Mapping[str, str], name: str) -> bool:
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


def _zone_from(env: Mapping[str, str], name: str) -> str | None:
    """Read and validate an IANA zone name.

    A wrong timezone silently shifts every timestamp we emit and would not be
    noticed for months, so an unresolvable zone fails at startup instead.
    """
    value = env.get(name, "").strip()
    if not value:
        return None
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ConfigError(f"{name} is not a known IANA timezone: {value!r}") from exc
    return value


def _backfill_start_date_from(env: Mapping[str, str]) -> str:
    """Validate the way GarminDB will parse it: an unparseable *_date value reaches
    GarminConnectConfigManager's sys.exit(-1)."""
    value = env.get("GARMIN_BACKFILL_START_DATE", "").strip()
    if not value:
        return DEFAULT_BACKFILL_START_DATE
    try:
        dateutil.parser.parse(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"GARMIN_BACKFILL_START_DATE is not a parseable date: {value!r}") from exc
    return value


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from ``env`` (defaults to ``os.environ``)."""
    env = os.environ if env is None else env

    # Cloud in a Bottle renamed OPENHOST_* to BOTTLE_*. Routers from 2026-08-27 on
    # export both; older ones export only the old name. Both are accepted with the new
    # one winning -- which is exactly why the image must never bake in a BOTTLE_ value.
    app_data_dir = (
        _path_from(env, "BOTTLE_APP_DATA_DIR", "OPENHOST_APP_DATA_DIR") or DEFAULT_APP_DATA_DIR
    )

    domain = env.get("GARMIN_DOMAIN", "").strip() or "garmin.com"
    if domain not in SUPPORTED_DOMAINS:
        raise ConfigError(f"GARMIN_DOMAIN must be one of {SUPPORTED_DOMAINS}, got {domain!r}")

    return Settings(
        app_data_dir=app_data_dir,
        garmin_domain=domain,
        home_tz=_zone_from(env, "GARMIN_HOME_TZ"),
        # The TZ the existing GarminDB corpus was imported under, if it was not the
        # home zone. Without it the skew is learned from the data instead.
        import_tz=_zone_from(env, "GARMIN_IMPORT_TZ"),
        backfill_start_date=_backfill_start_date_from(env),
        sync_interval_seconds=_sync_interval_from(env),
        fill_stage_gaps=_flag_from(env, "GARMIN_FILL_STAGE_GAPS"),
        derive_restless_periods=_flag_from(env, "GARMIN_DERIVE_RESTLESS_PERIODS"),
    )
