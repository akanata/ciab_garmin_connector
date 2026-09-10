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
- ``sleep_events.timestamp``, ``monitoring_hr.timestamp``,
  ``monitoring_hrv_value.timestamp`` and ``monitoring_rr.timestamp`` are naive
  **device-local** (``startGMT + utc_offset``), always on the home clock.
- ``sleep.day``, ``resting_hr.day``, ``hrv.day`` are naive local midnight of the
  Garmin calendar date.

The night it writes is internally coherent, which is what lets the derived
scalars be asserted against arithmetic rather than against magic numbers: 8 hours
in bed, 8 one-hour stages summing to 3h light + 2h deep + 2h REM + 1h awake, so
7h total sleep and 87.5% efficiency. Exactly one awake period is strictly interior
to the night.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from zoneinfo import ZoneInfo

from garmindb.garmindb import Attributes
from garmindb.garmindb import DailySummary
from garmindb.garmindb import GarminDb
from garmindb.garmindb import Hrv
from garmindb.garmindb import MonitoringDb
from garmindb.garmindb import MonitoringHeartRate
from garmindb.garmindb import MonitoringHrvStatus
from garmindb.garmindb import MonitoringHrvValue
from garmindb.garmindb import MonitoringRespirationRate
from garmindb.garmindb import RestingHeartRate
from garmindb.garmindb import Sleep
from garmindb.garmindb import SleepEvents
from idbutils import DbParams

HOME_TZ_NAME = "America/Denver"
HOME_TZ = ZoneInfo(HOME_TZ_NAME)

# A night runs 23:00 -> 07:00 on the home clock, labelled with the wake day, which
# is how Garmin's calendarDate works.
BEDTIME = dt.time(23, 0)
WAKE_TIME = dt.time(7, 0)
NIGHT_HOURS = 8

# One hour per entry. Chosen so the stage totals equal the Sleep row's columns and
# so exactly one awake period is strictly interior to the night.
STAGES: tuple[str, ...] = (
    "light_sleep",
    "deep_sleep",
    "deep_sleep",
    "awake",
    "light_sleep",
    "rem_sleep",
    "rem_sleep",
    "light_sleep",
)
# What STAGES adds up to, and what the Sleep row therefore records.
TOTAL_SLEEP = dt.time(7, 0)
DEEP_SLEEP = dt.time(2, 0)
LIGHT_SLEEP = dt.time(3, 0)
REM_SLEEP = dt.time(2, 0)
AWAKE = dt.time(1, 0)

# The FIT vocabulary and the two JSON vocabularies, for the same shape of night.
FIT_STAGES: tuple[str, ...] = STAGES
JSON_REM_STAGES: tuple[str, ...] = STAGES
JSON_NON_REM_STAGES: tuple[str, ...] = tuple(
    "more_awake" if s == "awake" else ("light_sleep" if s == "rem_sleep" else s) for s in STAGES
)

HEART_RATE_ROWS = 240
HEART_RATE_INTERVAL = dt.timedelta(minutes=2)
HRV_ROWS = 96
HRV_INTERVAL = dt.timedelta(minutes=5)
RESPIRATION_ROWS = 48
RESPIRATION_INTERVAL = dt.timedelta(minutes=10)


def heart_rate_at(index: int) -> int:
    """The value ``monitoring_hr`` row ``index`` carries. 50..69, min 50."""
    return 50 + (index % 20)


def hrv_at(index: int) -> float:
    """The value ``monitoring_hrv_value`` row ``index`` carries, RMSSD in ms."""
    return 40.0 + (index % 10) * 0.5


def respiration_at(index: int) -> float:
    return 13.0 + (index % 4) * 0.25


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


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

    @property
    def session_id(self) -> str:
        """The Container.id this night should be served under."""
        return f"garmin:sleep:{self.wake_day.isoformat()}"


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


def build_fixture(  # noqa: PLR0913 - a corpus has many independent dimensions
    tmp_path: Path,
    *,
    import_tz: dt.tzinfo = HOME_TZ,
    nights: int = 3,
    last_wake_day: dt.date = dt.date(2026, 6, 15),
    with_events: bool = True,
    stages: Sequence[str] = STAGES,
    event_overlap: dt.timedelta = dt.timedelta(0),
    null_start: bool = False,
    null_end: bool = False,
    total_sleep: dt.time = TOTAL_SLEEP,
    stage_durations: bool = True,
    sleep_score: int | None = None,
    avg_rr: float | None = None,
    stored_time_zone: str | None = HOME_TZ_NAME,
    heart_rate: bool = False,
    hrv: bool = False,
    hrv_status: bool = False,
    daily_hrv: bool = False,
    respiration: bool = False,
    resting_hr: bool = False,
    daily_summary_rhr: bool = False,
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
        for index, night in enumerate(night_list):
            row: dict[str, object] = {
                "day": night.day_column,
                "start": None if null_start else night.start_as_imported(import_tz),
                "end": None if null_end else night.end_as_imported(import_tz),
                "total_sleep": total_sleep,
            }
            if stage_durations:
                row |= {
                    "deep_sleep": DEEP_SLEEP,
                    "light_sleep": LIGHT_SLEEP,
                    "rem_sleep": REM_SLEEP,
                    "awake": AWAKE,
                }
            if sleep_score is not None:
                row["score"] = sleep_score
            if avg_rr is not None:
                row["avg_rr"] = avg_rr
            Sleep.s_insert_or_update(session, row, ignore_none=False)

            if with_events:
                # Contiguous hourly stages, device-local, starting at the same
                # instant sleep.start describes. event_overlap makes each one
                # overrun its successor, which is what a clock adjustment does.
                for hour, event in enumerate(stages):
                    SleepEvents.s_insert_or_update(
                        session,
                        {
                            "timestamp": night.first_event_local + dt.timedelta(hours=hour),
                            "event": event,
                            "duration": (
                                dt.datetime.min + dt.timedelta(hours=1) + event_overlap
                            ).time(),
                        },
                    )

            if resting_hr:
                RestingHeartRate.s_insert_or_update(
                    session,
                    {"day": night.day_column, "resting_heart_rate": 52.0 + index},
                )
            if daily_summary_rhr:
                DailySummary.s_insert_or_update(
                    session, {"day": night.day_column, "rhr": 60 + index}
                )
            if daily_hrv:
                Hrv.s_insert_or_update(
                    session, {"day": night.day_column, "last_night_avg": 38 + index}
                )

    # A zero-night corpus is a real case (nothing synced yet), so nothing below
    # may assume there is a newest night.
    newest = night_list[-1] if night_list else None
    with monitoring_db.managed_session() as session:
        if heart_rate and newest is not None:
            for index in range(HEART_RATE_ROWS):
                MonitoringHeartRate.s_insert_or_update(
                    session,
                    {
                        "timestamp": newest.first_event_local + HEART_RATE_INTERVAL * index,
                        "heart_rate": heart_rate_at(index),
                    },
                )
        if hrv and newest is not None:
            for index in range(HRV_ROWS):
                MonitoringHrvValue.s_insert_or_update(
                    session,
                    {
                        "timestamp": newest.first_event_local + HRV_INTERVAL * index,
                        "hrv": hrv_at(index),
                    },
                )
        if respiration and newest is not None:
            for index in range(RESPIRATION_ROWS):
                MonitoringRespirationRate.s_insert_or_update(
                    session,
                    {
                        "timestamp": newest.first_event_local + RESPIRATION_INTERVAL * index,
                        "rr": respiration_at(index),
                    },
                )
        if hrv_status:
            for night in night_list:
                MonitoringHrvStatus.s_insert_or_update(
                    session,
                    {
                        "timestamp": night.first_event_local + dt.timedelta(hours=1),
                        "last_night_average": 44.0,
                    },
                )

    return Fixture(
        db_dir=db_dir, garmin_db=garmin_db, monitoring_db=monitoring_db, nights=night_list
    )
