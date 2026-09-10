"""Render and validate GarminConnectConfig.json.

GarminConnectConfigManager.__init__ catches every exception from its JSON load and
calls ``sys.exit(-1)``. In a CLI that is a usable error message; in a server process
it is an unrecoverable exit that no handler can intercept. So the file is always
rendered and validated here first, and the manager is only constructed once the
document is known to load cleanly.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import dateutil.parser
from garmindb import GarminConnectConfigManager

from garmin_health.config import Settings

REQUIRED_SECTIONS = ("db", "garmin", "credentials", "data", "directories", "enabled_stats")


class InvalidGarminConfig(Exception):
    """The GarminDB config is absent or malformed."""


def render_config(settings: Settings, *, user: str = "") -> dict[str, Any]:
    """Build the config document GarminDB will read.

    The password is deliberately always empty. We perform the Garmin login
    ourselves and hand GarminDB the resulting ``garmin_tokens.json``, so the
    owner's plaintext password never has to be written to disk at all.
    """
    return {
        "db": {"type": "sqlite"},
        "garmin": {"domain": settings.garmin_domain},
        "credentials": {
            "user": user,
            "password": "",
            "secure_password": False,
            "password_file": None,
        },
        "data": {
            "sleep_start_date": settings.backfill_start_date,
            "monitoring_start_date": settings.backfill_start_date,
            "rhr_start_date": settings.backfill_start_date,
            "hrv_start_date": settings.backfill_start_date,
            "weight_start_date": settings.backfill_start_date,
            "download_latest_activities": 0,
            "download_all_activities": 0,
        },
        # relative_to_home: false plus an absolute base_dir is the only way to place
        # the corpus under app data. GarminConnectConfigManager.homedir is a CLASS
        # attribute evaluated at import, so exporting HOME later has no effect.
        "directories": {
            "relative_to_home": False,
            "base_dir": str(settings.health_data_dir.resolve()),
        },
        # Activities are by far the slowest download and are out of scope, as are
        # steps/itime/weight for this iteration.
        "enabled_stats": {
            "monitoring": True,
            "sleep": True,
            "rhr": True,
            "hrv": True,
            "steps": False,
            "itime": False,
            "weight": False,
            "activities": False,
        },
        "course_views": {"steps": []},
        "modes": {},
        "activities": {"display": []},
        "settings": {"metric": True, "default_display_activities": []},
        "checkup": {"look_back_days": 90},
    }


def _check_dates(node: Mapping[str, Any], path: str) -> None:
    """Every ``*_date`` key must survive JsonConfig's object_hook.

    That hook runs ``dateutil.parser.parse`` on any key ending in ``_date``; a
    failure there is what reaches GarminConnectConfigManager's sys.exit(-1).
    """
    for key, value in node.items():
        if isinstance(value, Mapping):
            _check_dates(value, f"{path}.{key}")
        elif str(key).endswith("_date"):
            try:
                dateutil.parser.parse(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise InvalidGarminConfig(
                    f"{path}.{key} is not a parseable date: {value!r}"
                ) from exc


def validate_config(raw: object) -> None:
    """Raise InvalidGarminConfig unless GarminDB will load ``raw`` without exiting."""
    if not isinstance(raw, Mapping):
        raise InvalidGarminConfig(f"config must be a JSON object, got {type(raw).__name__}")

    for section in REQUIRED_SECTIONS:
        if section not in raw:
            raise InvalidGarminConfig(f"config is missing the {section!r} section")
        if not isinstance(raw[section], Mapping):
            raise InvalidGarminConfig(f"config section {section!r} must be a JSON object")

    if "user" not in raw["credentials"]:
        raise InvalidGarminConfig("credentials section is missing 'user'")

    directories = raw["directories"]
    if directories.get("relative_to_home", True):
        raise InvalidGarminConfig("directories.relative_to_home must be false")
    base_dir = directories.get("base_dir")
    if not isinstance(base_dir, str) or not Path(base_dir).is_absolute():
        raise InvalidGarminConfig(
            f"directories.base_dir must be an absolute path, got {base_dir!r}"
        )

    _check_dates(raw, "config")


def _atomic_write(path: Path, text: str) -> None:
    """Write owner-only, replacing atomically so no reader sees a partial file."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_config(settings: Settings) -> dict[str, Any]:
    """Return the on-disk config, or raise InvalidGarminConfig."""
    try:
        raw = json.loads(settings.garmin_config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidGarminConfig(f"cannot read {settings.garmin_config_file}: {exc}") from exc
    validate_config(raw)
    return dict(raw)


def config_user(settings: Settings) -> str | None:
    """The Garmin account recorded in the config, or None if absent/unreadable."""
    try:
        raw = read_config(settings)
    except InvalidGarminConfig:
        return None
    user = raw["credentials"].get("user")
    return user or None


def ensure_config(settings: Settings, *, user: str | None = None) -> Path:
    """Write a valid config, preserving the recorded user unless one is given."""
    if user is None:
        user = config_user(settings) or ""
    document = render_config(settings, user=user)
    validate_config(document)
    _atomic_write(settings.garmin_config_file, json.dumps(document, indent=4))
    return settings.garmin_config_file


def load_manager(settings: Settings, *, repair: bool = True) -> GarminConnectConfigManager:
    """Return a GarminConnectConfigManager, never letting it reach sys.exit(-1)."""
    if repair:
        ensure_config(settings)
    else:
        read_config(settings)
    return GarminConnectConfigManager(str(settings.config_dir))
