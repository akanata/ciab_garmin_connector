"""A real GarminDB SQLite corpus in a tmpdir, built from nothing.

``DB.__init__`` runs ``create_all``, so constructing ``GarminDb(DbParams(...))``
against an empty directory produces a complete, valid schema. That means no Garmin
account, no checked-in binary fixture, and no schema SQL to keep in sync across
GarminDB upgrades.

The point of this fixture is to reproduce GarminDB's **four clocks** faithfully, so
the timezone strategy can be tested against the real skew rather than a mock:

- ``sleep.start`` / ``sleep.end`` are written naive in the *importing container's*
  TZ, because GarminDB parses them with ``datetime.fromtimestamp(ms/1000)``, which
  renders in whatever ``TZ`` the import process ran under. That is what
  ``import_tz`` simulates: pass ``timezone.utc`` to reproduce the skew on demand.
- ``sleep_events.timestamp`` and ``monitoring_hr.timestamp`` are naive
  **device-local** (``startGMT + utc_offset``), always on the home clock.
- ``sleep.day`` is naive local midnight of the Garmin calendar date.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from zoneinfo import ZoneInfo

from garmindb.garmindb import Attributes
from garmindb.garmindb import GarminDb
from garmindb.garmindb import MonitoringDb
from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import Sleep
from garmindb.garmindb import SleepEvents
from idbutils import DbParams

HOME_TZ_NAME = "America/Denver"
HOME_TZ = ZoneInfo(HOME_TZ_NAME)

# A night runs 23:00 -> 07:00 on the home clock, labelled with the wake day, which
# is how Garmin's calendarDate works.
BEDTIME = dt.time(23, 0)
WAKE_TIME = dt.time(7, 0)
STAGES = ("deep_sleep", "light_sleep", "rem_sleep", "awake")


@dataclass(frozen=True)
class Night:
    """One night, described by the true instants it happened at."""

    wake_day: dt.date
    start_utc: dt.datetime
    end_utc: dt.datetime

    @property
    def day_column(self) -> dt.datetime:
        """What GarminDB stores in sleep.day: naive local midnight of the wake day."""
        return dt.datetime.combine(self.wake_day, dt.time.min)

    def start_as_imported(self, import_tz: dt.tzinfo) -> dt.datetime:
        """sleep.start as rendered by an importer running under ``import_tz``."""
        return self.start_utc.astimezone(import_tz).replace(tzinfo=None)

    def end_as_imported(self, import_tz: dt.tzinfo) -> dt.datetime:
        return self.end_utc.astimezone(import_tz).replace(tzinfo=None)

    @property
    def first_event_local(self) -> dt.datetime:
        """The first sleep_events row: the same instant as start, device-local."""
        return self.start_utc.astimezone(HOME_TZ).replace(tzinfo=None)


@dataclass
class Fixture:
    db_dir: Path
    garmin_db: GarminDb
    monitoring_db: MonitoringDb
    nights: list[Night] = field(default_factory=list)

    @property
    def newest(self) -> Night:
        return self.nights[-1]


def make_nights(count: int, last_wake_day: dt.date) -> list[Night]:
    """``count`` consecutive nights ending on ``last_wake_day``.

    Wall times are fixed on the home clock and converted to real instants, so a
    night spanning a DST transition genuinely is 7 or 9 hours long.
    """
    nights = []
    for index in range(count):
        wake_day = last_wake_day - dt.timedelta(days=count - 1 - index)
        bed_local = dt.datetime.combine(wake_day - dt.timedelta(days=1), BEDTIME)
        wake_local = dt.datetime.combine(wake_day, WAKE_TIME)
        nights.append(
            Night(
                wake_day=wake_day,
                start_utc=bed_local.replace(tzinfo=HOME_TZ).astimezone(dt.UTC),
                end_utc=wake_local.replace(tzinfo=HOME_TZ).astimezone(dt.UTC),
            )
        )
    return nights


def build_fixture(
    tmp_path: Path,
    *,
    import_tz: dt.tzinfo = HOME_TZ,
    nights: int = 3,
    last_wake_day: dt.date = dt.date(2026, 6, 15),
    with_events: bool = True,
    null_start: bool = False,
    stored_time_zone: str | None = HOME_TZ_NAME,
    heart_rate: bool = False,
) -> Fixture:
    """Build a GarminDB corpus. ``import_tz`` is the TZ the importer ran under."""
    db_dir = tmp_path / "DBs"
    db_dir.mkdir(parents=True, exist_ok=True)
    params = DbParams(db_type="sqlite", db_path=str(db_dir))

    # Both DB objects must exist before any table classmethod is touched: setup()
    # runs inside DB.__init__ and is what installs cls.time_col.
    garmin_db = GarminDb(params)
    monitoring_db = MonitoringDb(params)

    if stored_time_zone is not None:
        Attributes.set(garmin_db, "time_zone", stored_time_zone)

    night_list = make_nights(nights, last_wake_day)

    with garmin_db.managed_session() as session:
        for night in night_list:
            Sleep.s_insert_or_update(
                session,
                {
                    "day": night.day_column,
                    "start": None if null_start else night.start_as_imported(import_tz),
                    "end": None if null_start else night.end_as_imported(import_tz),
                    "total_sleep": dt.time(7, 0),
                },
                ignore_none=False,
            )
            if not with_events:
                continue
            # Eight contiguous hourly stages, device-local, starting at the same
            # instant sleep.start describes.
            for hour in range(8):
                SleepEvents.s_insert_or_update(
                    session,
                    {
                        "timestamp": night.first_event_local + dt.timedelta(hours=hour),
                        "event": STAGES[hour % len(STAGES)],
                        "duration": dt.time(1, 0),
                    },
                )

    if heart_rate:
        newest = night_list[-1]
        with monitoring_db.managed_session() as session:
            for index in range(240):
                MonitoringHeartRate.s_insert_or_update(
                    session,
                    {
                        "timestamp": newest.first_event_local + dt.timedelta(minutes=2 * index),
                        "heart_rate": 50 + (index % 20),
                    },
                )

    return Fixture(
        db_dir=db_dir, garmin_db=garmin_db, monitoring_db=monitoring_db, nights=night_list
    )
