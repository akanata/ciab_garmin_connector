"""Import scope the owner controls: how far back to go, and which metrics.

These are the two knobs that decide how long a sync runs, and both used to be
reachable only by restarting the container with different environment variables.
They live here rather than in :mod:`garmin_health.config` because they are
*persisted state the owner edits*, not environment the operator sets --
``config.py`` stays pure and does no I/O.

The environment still seeds the defaults: ``GARMIN_BACKFILL_START_DATE`` is what
a container with no saved preferences uses. Once the owner saves, the file wins,
because a setting that silently reverted on the next restart would be worse than
no setting at all.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
from collections.abc import Iterable
from pathlib import Path

import attrs
import dateutil.parser

from garmin_health.config import DEFAULT_SYNC_INTERVAL_SECONDS
from garmin_health.config import Settings

logger = logging.getLogger(__name__)

# GarminDB knows eight statistics; ``garmin/ingest.py`` implements download
# branches for these four. Offering the rest would be a checkbox that does
# nothing, and Statistics.from_string would raise on anything not in its enum.
DOWNLOADABLE_STATS: tuple[str, ...] = ("monitoring", "sleep", "rhr", "hrv")

# What the owner can choose on /setup. A fixed set rather than a free number, so
# neither a typo nor a hand-crafted POST can make the sync hit Garmin every
# minute; fifteen minutes is already about as often as a watch syncs to Garmin
# Connect, so shorter would fetch nothing new.
SYNC_INTERVAL_CHOICES: tuple[int, ...] = (
    15 * 60,
    30 * 60,
    60 * 60,
    3 * 60 * 60,
    6 * 60 * 60,
    12 * 60 * 60,
    24 * 60 * 60,
)


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def sync_interval_label(seconds: int) -> str:
    """ "Every hour", "Every 30 minutes" -- how the page names an interval."""
    if seconds == 60 * 60:
        return "Every hour"
    if seconds % (60 * 60) == 0:
        return f"Every {_plural(seconds // (60 * 60), 'hour')}"
    if seconds % 60 == 0:
        return f"Every {_plural(seconds // 60, 'minute')}"
    return f"Every {_plural(seconds, 'second')}"


# What each stat is called in the owner's language, and what it actually brings
# in -- "rhr" and "monitoring" mean nothing to someone reading their own page.
STAT_LABELS: dict[str, str] = {
    "monitoring": "Heart rate",
    "sleep": "Sleep",
    "rhr": "Resting heart rate",
    "hrv": "Heart rate variability",
}

STAT_DETAIL: dict[str, str] = {
    "monitoring": "Continuous heart rate, roughly one reading every two minutes. By far the largest and slowest metric.",
    "sleep": "Sleep sessions and their stage timelines.",
    "rhr": "One resting heart rate reading per day.",
    "hrv": "Overnight heart rate variability.",
}


class InvalidPreferences(Exception):
    """The submitted import scope is not one this service can act on."""


@attrs.frozen
class ImportPreferences:
    """How far back to download, and which statistics to download at all."""

    start_date: dt.date
    enabled_stats: frozenset[str]
    # How often the background sync runs. Kept on the same saved document because
    # it is the third knob deciding how much importing costs, and
    # SYNC_INTERVAL_SECONDS seeds it the way GARMIN_BACKFILL_START_DATE seeds the
    # start date.
    sync_interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS

    @property
    def start_date_text(self) -> str:
        """The form GarminConnectConfig.json carries.

        ``GarminConnectConfigManager`` runs ``dateutil.parser.parse`` on every key
        ending in ``_date`` and reaches ``sys.exit(-1)`` if it fails, so this has
        to be something dateutil accepts. ISO 8601 always is.
        """
        return self.start_date.isoformat()

    def is_enabled(self, stat: str) -> bool:
        return stat in self.enabled_stats

    def as_dict(self) -> dict[str, object]:
        return {
            "start_date": self.start_date_text,
            # Sorted so the file does not churn between saves that changed nothing.
            "enabled_stats": sorted(self.enabled_stats),
            "sync_interval_seconds": self.sync_interval_seconds,
        }

    @classmethod
    def defaults(cls, settings: Settings) -> ImportPreferences:
        return cls(
            start_date=_parse_date(settings.backfill_start_date) or dt.date(2019, 12, 31),
            enabled_stats=frozenset(DOWNLOADABLE_STATS),
            sync_interval_seconds=settings.sync_interval_seconds,
        )


def _parse_date(raw: object) -> dt.date | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed: dt.date = dateutil.parser.parse(raw.strip()).date()
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed


def load_preferences(settings: Settings) -> ImportPreferences:
    """Read the saved scope, falling back to the environment-seeded defaults.

    Never raises. A corrupt preferences file must not take out ``/setup``, which
    is the only place the owner could fix it from.
    """
    defaults = ImportPreferences.defaults(settings)
    try:
        raw = json.loads(settings.preferences_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaults
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not read the import preferences at %s (%s); using defaults.",
            settings.preferences_file,
            exc,
        )
        return defaults

    if not isinstance(raw, dict):
        logger.warning("Import preferences file is not a JSON object; using defaults.")
        return defaults

    start_date = _parse_date(raw.get("start_date"))
    if start_date is None:
        logger.warning(
            "Import preferences hold an unusable start_date %r; using %s.",
            raw.get("start_date"),
            defaults.start_date,
        )
        start_date = defaults.start_date

    stored = raw.get("enabled_stats")
    if not isinstance(stored, list):
        enabled = defaults.enabled_stats
    else:
        # Unknown names are dropped rather than passed through: a hand-edit or a
        # downgrade would otherwise reach Statistics.from_string and raise deep
        # inside GarminDB, long after anyone could connect it to this file.
        enabled = frozenset(s for s in stored if s in DOWNLOADABLE_STATS)
        for name in stored:
            if name not in DOWNLOADABLE_STATS:
                logger.warning("Ignoring unknown statistic %r in the import preferences.", name)

    stored_interval = raw.get("sync_interval_seconds")
    if stored_interval is None:
        # A file saved before this setting existed.
        interval = defaults.sync_interval_seconds
    elif (
        # bool is an int subclass: a stored `true` would otherwise pass as 1 second.
        isinstance(stored_interval, bool)
        or not isinstance(stored_interval, int)
        or stored_interval <= 0
    ):
        logger.warning(
            "Import preferences hold an unusable sync_interval_seconds %r; using %s.",
            stored_interval,
            defaults.sync_interval_seconds,
        )
        interval = defaults.sync_interval_seconds
    else:
        interval = stored_interval

    return ImportPreferences(
        start_date=start_date, enabled_stats=enabled, sync_interval_seconds=interval
    )


def save_preferences(settings: Settings, preferences: ImportPreferences) -> None:
    """Persist the scope, replacing atomically so no reader sees a partial file."""
    path = settings.preferences_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        tmp.write_text(json.dumps(preferences.as_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(
        "Import scope saved: from %s, statistics %s.",
        preferences.start_date_text,
        sorted(preferences.enabled_stats) or "(none)",
    )


def parse_preferences(
    settings: Settings,
    *,
    start_date: str,
    stats: Iterable[str],
    sync_interval: str | None = None,
    today: dt.date | None = None,
) -> ImportPreferences:
    """Validate owner-submitted form input. Raises :class:`InvalidPreferences`.

    Stricter than :func:`load_preferences` on purpose: a value that came from a
    person needs to be rejected with an explanation, whereas a value already on
    disk needs to degrade to something serviceable.
    """
    today = today or dt.date.today()

    # Strict ISO 8601, unlike the lenient dateutil pass used for stored and
    # environment values. dateutil accepts "03/01/2024" and resolves it as March
    # 1st on a US default, which is not what an owner outside the US means -- and
    # a start date that is silently three months off is exactly the kind of error
    # nobody notices until the download has already run.
    try:
        parsed = dt.date.fromisoformat(start_date.strip())
    except (AttributeError, ValueError):
        raise InvalidPreferences(
            f"{start_date!r} is not a date this service can read. Use YYYY-MM-DD."
        ) from None
    if parsed > today:
        # Every stat's span is (today - start), so a future date asks Garmin for a
        # negative number of days and downloads nothing, silently and for ever.
        raise InvalidPreferences(
            f"The earliest import date cannot be in the future ({parsed.isoformat()})."
        )

    selected = list(stats)
    unknown = [s for s in selected if s not in DOWNLOADABLE_STATS]
    if unknown:
        # Dropping it silently would leave the owner believing they enabled it.
        raise InvalidPreferences(f"Not a metric this service can download: {', '.join(unknown)}.")

    if sync_interval is None:
        # Not submitted: keep whatever is saved rather than resetting it.
        interval = load_preferences(settings).sync_interval_seconds
    else:
        try:
            interval = int(str(sync_interval).strip())
        except ValueError:
            raise InvalidPreferences(
                f"{sync_interval!r} is not a sync interval this service offers."
            ) from None
        if interval <= 0:
            raise InvalidPreferences("A sync interval has to be a positive number of seconds.")
        # The operator's own SYNC_INTERVAL_SECONDS is always acceptable, or
        # re-saving the page would be refused whenever it names a value the form
        # does not list.
        if interval not in SYNC_INTERVAL_CHOICES and interval != settings.sync_interval_seconds:
            raise InvalidPreferences(
                f"Choose how often to sync from the list offered; "
                f"{sync_interval_label(interval).lower()} is not one of them."
            )

    return ImportPreferences(
        start_date=parsed, enabled_stats=frozenset(selected), sync_interval_seconds=interval
    )


def preferences_path(settings: Settings) -> Path:
    return settings.preferences_file
