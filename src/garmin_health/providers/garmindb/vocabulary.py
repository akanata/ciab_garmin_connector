"""``sleep_events.event`` -> ``SleepStage``. A pure dict lookup, plus one warning.

GarminDB writes that column from two different ingest paths with two different
enums, and stores ``enum.name`` in both -- so the values are lowercase snake_case
strings, and both vocabularies have to be mapped:

- ``fitfile.field_enums.SleepActivityLevel`` from FIT files off the watch.
- ``garmindb.import_monitoring.SleepActivityLevels`` (no REM) and
  ``RemSleepActivityLevels`` (REM) from Garmin Connect's JSON.

Neither enum is imported here. Depending on them at runtime would tie the mapping
to whatever those packages happen to define today, and a silently *renamed* member
would then map to nothing while the code still looked correct. The literal tokens
are written out instead, and ``tests/test_vocabulary.py`` reads the live enums and
fails if either grows a member this table does not cover.
"""

from __future__ import annotations

import logging

from health_data_service import SleepStage

from garmin_health.warn_once import WarnOnce

logger = logging.getLogger(__name__)

_STAGE_BY_EVENT: dict[str, SleepStage] = {
    # fitfile.field_enums.SleepActivityLevel -- the FIT path.
    "unknown": SleepStage.UNKNOWN,
    "awake": SleepStage.AWAKE,
    "light_sleep": SleepStage.LIGHT,
    "deep_sleep": SleepStage.DEEP,
    "rem_sleep": SleepStage.REM,
    # garmindb.import_monitoring.SleepActivityLevels -- JSON, non-REM device.
    "more_awake": SleepStage.AWAKE,
    # garmindb.import_monitoring.RemSleepActivityLevels -- JSON, REM device.
    "unmeasurable": SleepStage.UNKNOWN,
    # Legacy rows. SleepEvents.get_wake_time() still queries for this token.
    "wake_time": SleepStage.AWAKE,
}


def known_events() -> frozenset[str]:
    """Every token this module maps. Used by the tests that read the live enums."""
    return frozenset(_STAGE_BY_EVENT)


# One WARNING per distinct token, ever. A night is hundreds of events, so warning
# per row would bury the signal in its own noise -- and the entire point is to
# make a new Garmin vocabulary visible instead of letting it degrade silently to
# UNKNOWN. Given this module's own logger, so the warning still names the file
# that has to be edited.
_unmapped = WarnOnce(
    logger,
    "Unmapped Garmin sleep event %r; reporting it as UNKNOWN. Garmin has probably added a "
    "sleep level -- add it to garmin_health.providers.garmindb.vocabulary.",
)

#: Forget which tokens have been warned about. A test seam.
reset_unknown_event_log = _unmapped.reset


def stage_for_event(event: str | None) -> SleepStage:
    """Map one ``sleep_events.event`` value, degrading to UNKNOWN with a warning.

    ``event`` is nullable in the schema, so ``None`` is reachable from real data
    and is treated as its own unmapped token.
    """
    normalized = "" if event is None else event.strip().lower()
    stage = _STAGE_BY_EVENT.get(normalized)
    if stage is None:
        _unmapped(normalized)
        return SleepStage.UNKNOWN
    return stage
