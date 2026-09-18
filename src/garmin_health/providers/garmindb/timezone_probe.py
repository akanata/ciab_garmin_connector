"""Read the timezone facts out of GarminDB and turn them into a TimeZonePolicy.

The policy itself is pure and lives in ``garmin_health.providers.garmindb.timezones``; this module is
the part that knows about tables.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from garmindb.garmindb import Attributes
from garmindb.garmindb import GarminDb
from garmindb.garmindb import Sleep
from garmindb.garmindb import SleepEvents

from garmin_health.providers.garmindb.timezones import DEFAULT_PROBE_NIGHTS
from garmin_health.providers.garmindb.timezones import TimeZonePolicy
from garmin_health.providers.garmindb.timezones import TimeZoneUnresolved
from garmin_health.providers.garmindb.timezones import learn_import_offset
from garmin_health.providers.garmindb.timezones import resolve_home_tz

logger = logging.getLogger(__name__)

# Anchored on sleep.day rather than sleep.start, because day and sleep_events are
# both on the home clock: the window therefore does not move with the very offset
# we are trying to learn. +/-12h spans a normal night (asleep after noon of the
# previous day) while excluding the adjacent nights entirely.
EVENT_WINDOW = dt.timedelta(hours=12)


def read_stored_time_zone(garmin_db: GarminDb) -> str | None:
    """The raw ``attributes.time_zone`` value, uninterpreted.

    It may be an IANA name (written by GarminPersonalInformation) or a stringified
    FIT enum (written by fit_file_processor) -- same key, last writer wins.
    Deciding which is usable is resolve_home_tz's job.
    """
    value: str | None = Attributes.get_string(garmin_db, "time_zone")
    return value


def read_offset_pairs(
    garmin_db: GarminDb,
    *,
    nights: int = DEFAULT_PROBE_NIGHTS,
    window: dt.timedelta = EVENT_WINDOW,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Pair each recent night's ``sleep.start`` with its first ``sleep_events`` row.

    Both describe the same instant, on the importer's clock and the device's clock
    respectively, so their difference is the skew to be learned.
    """
    pairs: list[tuple[dt.datetime, dt.datetime]] = []
    with garmin_db.managed_session() as session:
        recent = (
            session.query(Sleep.day, Sleep.start)
            .filter(Sleep.start.is_not(None))
            .order_by(Sleep.day.desc())
            .limit(nights)
            .all()
        )
        for day, start in reversed(recent):
            # Ordering and taking the first keeps SQLAlchemy's DateTime result
            # processor in play, which a func.min() would bypass.
            first_event = (
                session.query(SleepEvents.timestamp)
                .filter(SleepEvents.timestamp >= day - window)
                .filter(SleepEvents.timestamp < day + window)
                .order_by(SleepEvents.timestamp.asc())
                .first()
            )
            if first_event is not None:
                pairs.append((start, first_event[0]))
    return pairs


def _has_importable_sleep_rows(garmin_db: GarminDb) -> bool:
    with garmin_db.managed_session() as session:
        return session.query(Sleep.day).filter(Sleep.start.is_not(None)).first() is not None


def resolve_policy(
    garmin_db: GarminDb,
    *,
    configured_home_tz: str | None,
    configured_import_tz: str | None,
) -> TimeZonePolicy:
    """Build the corpus's TimeZonePolicy. Called once at startup."""
    home_tz = resolve_home_tz(
        configured=configured_home_tz, stored=read_stored_time_zone(garmin_db)
    )

    if configured_import_tz and configured_import_tz.strip():
        try:
            import_tz = ZoneInfo(configured_import_tz.strip())
        except Exception as exc:
            raise TimeZoneUnresolved(
                f"GARMIN_IMPORT_TZ is not a known IANA timezone: {configured_import_tz!r}"
            ) from exc
        logger.info("Import timezone configured as %s; skipping offset learning.", import_tz)
        return TimeZonePolicy(home_tz=home_tz, import_tz=import_tz)

    learned = learn_import_offset(read_offset_pairs(garmin_db))
    if learned is None:
        if _has_importable_sleep_rows(garmin_db):
            logger.warning(
                "Could not learn the GarminDB import offset: no night has both a sleep.start and "
                "sleep events to compare. Assuming zero skew, which is wrong if the corpus was "
                "imported under a different TZ than %s. Set GARMIN_IMPORT_TZ to be certain.",
                home_tz,
            )
        return TimeZonePolicy(home_tz=home_tz)

    logger.info("Learned GarminDB import offset of %s against home zone %s.", learned, home_tz)
    return TimeZonePolicy(home_tz=home_tz, import_offset=learned)
