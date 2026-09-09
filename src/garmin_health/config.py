"""Settings read from the environment. No I/O beyond validating a timezone name."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

import attrs

DEFAULT_APP_DATA_DIR = Path("data")
DEFAULT_SYNC_INTERVAL_SECONDS = 6 * 60 * 60
SUPPORTED_DOMAINS = ("garmin.com", "garmin.cn")


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
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS

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
    def health_data_dir(self) -> Path:
        """GarminDB's base_dir: the raw JSON/FIT corpus plus the SQLite DBs."""
        return self.app_data_dir / "HealthData"

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


def settings_from_env(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from ``env`` (defaults to ``os.environ``)."""
    env = os.environ if env is None else env

    # Cloud in a Bottle renamed OPENHOST_* to BOTTLE_*, but deployed apps still
    # export only the old name, so both are accepted with the new one winning.
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
        sync_interval_seconds=_sync_interval_from(env),
    )
