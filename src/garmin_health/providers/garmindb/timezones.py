"""The only place naive<->aware conversion happens.

GarminDB stores **naive** datetimes on four different clocks:

===========================================  ==================================
Column(s)                                    The naive value means
===========================================  ==================================
``monitoring_hr``, ``monitoring_hrv_value``  device-local, from the offset
``monitoring_rr``, ``sleep_events``          recorded in the FIT file itself
``sleep.start``, ``sleep.end``               the *importing container's* ``TZ``
``sleep.day``, ``resting_hr.day``, ...       naive local midnight of the
                                             Garmin calendar date
===========================================  ==================================

So at the Docker default ``TZ=UTC``, ``sleep.start`` lands in UTC while
``sleep_events`` lands in the user's local time -- a multi-hour skew between two
tables written by the same importer. The strategy is one clock plus a learned
skew: everything converts through ``home_tz``, and only ``sleep.start`` and
``sleep.end`` additionally need the import skew removed.

Every failure mode here is a silent wrong answer rather than an exception, which
is why the conversions reject inputs of the wrong awareness instead of doing
something plausible with them.

Assumption: the account has one home timezone and the watch was in it. That is
wrong for travellers, and the failure mode is a fixed hours-shift on travel days.
Per-row offsets simply are not in the GarminDB schema.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from collections.abc import Iterable
from zoneinfo import ZoneInfo

import attrs

from garmin_health.errors import ProviderNotReady

logger = logging.getLogger(__name__)

# Every real UTC offset is a whole number of quarter hours, so the difference of
# two of them is too. Anything else is a first event that was not at sleep.start.
OFFSET_STEP = dt.timedelta(minutes=15)

# Real UTC offsets span -12:00..+14:00, so their difference cannot exceed 26h.
MAX_PLAUSIBLE_OFFSET = dt.timedelta(hours=26)

DEFAULT_PROBE_NIGHTS = 90


class TimeZoneUnresolved(ProviderNotReady):
    """The account's home timezone could not be determined.

    Deliberately on the *not ready* branch: before the first sync there is no
    stored zone and nothing to serve either, which is an empty 200 rather than a
    fault. Once there is data, the same failure is a 503 -- ``service.py`` is
    what tells those two apart.
    """


@attrs.frozen
class TimeZonePolicy:
    """How this corpus's naive datetimes map onto real instants.

    ``import_tz`` and ``import_offset`` are two ways to say the same thing about
    ``sleep.start``/``sleep.end``. The zone is exact and handles daylight saving
    across the whole corpus; the scalar is what can be *learned* from the data
    when nobody told us which zone the importer ran under.
    """

    home_tz: ZoneInfo
    import_offset: dt.timedelta = dt.timedelta(0)
    import_tz: ZoneInfo | None = None

    def to_utc(self, naive_local: dt.datetime) -> dt.datetime:
        """Device-local wall clock -> aware UTC. Every value we emit goes through this."""
        if naive_local.tzinfo is not None:
            # .replace(tzinfo=home_tz) would relabel an aware value, moving the
            # instant by hours without raising.
            raise ValueError(f"expected a naive device-local datetime, got aware {naive_local!r}")
        return naive_local.replace(tzinfo=self.home_tz).astimezone(dt.UTC)

    def to_naive_local(self, aware: dt.datetime) -> dt.datetime:
        """Aware -> naive device-local. Every query bound goes through this.

        SQLAlchemy's SQLite ``DATETIME`` bind processor formats ``.year``/``.hour``
        and DISCARDS ``tzinfo``, so an aware bound silently compares as a naive
        wall clock against naive local rows, selecting the wrong rows with no error.
        """
        if aware.tzinfo is None:
            # datetime.astimezone() on a naive value assumes the *system* zone,
            # which differs between a container and a developer's laptop.
            raise ValueError(f"expected an aware datetime, got naive {aware!r}")
        return aware.astimezone(self.home_tz).replace(tzinfo=None)

    def sleep_column_to_utc(self, naive_import: dt.datetime) -> dt.datetime:
        """``sleep.start``/``sleep.end`` -> aware UTC. Those two columns only.

        Everything else on the naive-local clock uses :meth:`to_utc` with no
        offset applied.
        """
        if naive_import.tzinfo is not None:
            raise ValueError(f"expected a naive imported datetime, got aware {naive_import!r}")
        if self.import_tz is not None:
            # Exactly inverts GarminDB's datetime.fromtimestamp(ms/1000), including
            # across daylight-saving changes a single scalar offset cannot follow.
            return naive_import.replace(tzinfo=self.import_tz).astimezone(dt.UTC)
        return self.to_utc(naive_import - self.import_offset)


def resolve_home_tz(*, configured: str | None, stored: str | None) -> ZoneInfo:
    """Resolve the account's home zone: ``GARMIN_HOME_TZ`` first, then what Garmin stored.

    There is deliberately no fallback to the container's local zone. A silent
    wrong answer corrupts every timestamp this service emits and would not be
    noticed for months; failing at boot with "set GARMIN_HOME_TZ" is a
    five-second fix.
    """
    if configured and configured.strip():
        try:
            return ZoneInfo(configured.strip())
        except Exception as exc:
            raise TimeZoneUnresolved(
                f"GARMIN_HOME_TZ is not a known IANA timezone: {configured!r}"
            ) from exc

    if stored and stored.strip():
        try:
            return ZoneInfo(stored.strip())
        except Exception as exc:
            # Two importers write attributes.time_zone under last-writer-wins:
            # GarminPersonalInformation writes IANA, fit_file_processor writes a
            # stringified FIT enum. Only the first is usable.
            raise TimeZoneUnresolved(
                f"Garmin stored {stored!r} as the account timezone, which is not an IANA zone "
                f"(GarminDB writes a FIT enum into that key too). Set GARMIN_HOME_TZ to the "
                f"account's home timezone."
            ) from exc

    raise TimeZoneUnresolved(
        "The Garmin account's home timezone is unknown. Set GARMIN_HOME_TZ, or run a sync "
        "so the account profile is imported."
    )


def learn_import_offset(
    pairs: Iterable[tuple[dt.datetime, dt.datetime]],
    *,
    step: dt.timedelta = OFFSET_STEP,
) -> dt.timedelta | None:
    """Learn the importer's skew from (``sleep.start``, first ``sleep_events``) pairs.

    Those two describe the same instant on two different clocks, so their naive
    difference *is* the skew. Noise is rejected by keeping only differences that
    are a whole quarter hour and plausible as a difference of UTC offsets, then
    taking the mode -- a handful of mispaired nights cannot outvote the truth.

    Returns ``None`` when nothing could be learned, which is distinct from a
    learned zero (the steady state, where the importer ran in the home zone).
    """
    candidates = []
    for start, first_event in pairs:
        delta = start - first_event
        if abs(delta) > MAX_PLAUSIBLE_OFFSET:
            continue
        if delta % step != dt.timedelta(0):
            continue
        candidates.append(delta)

    if not candidates:
        return None

    counts = Counter(candidates)
    best = max(counts.values())
    # Tie-break towards the smaller offset so row ordering cannot change the answer.
    return min(
        (delta for delta, count in counts.items() if count == best), key=lambda d: (abs(d), d)
    )
