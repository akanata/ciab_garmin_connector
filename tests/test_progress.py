"""What a running acquisition reports, and the shape ``/setup`` polls for.

Generic because the page is: whatever a provider is doing, it says so as a
label and an optional count, and the browser reads exactly those three keys.
"""

from __future__ import annotations

import attrs
import pytest

from garmin_health.progress import ProgressSink
from garmin_health.progress import SyncStep
from garmin_health.progress import no_progress


def test_a_step_renders_the_three_keys_the_page_reads() -> None:
    """BUSY_SCRIPT polls /sync/status and reads label, done and total off it.
    Renaming any of them silently empties the progress line."""
    step = SyncStep(label="Downloading sleep (3 days)", done=2, total=4)
    assert step.as_dict() == {"label": "Downloading sleep (3 days)", "done": 2, "total": 4}


def test_counts_are_optional() -> None:
    """A phase that cannot say how much work it has still has something to say."""
    assert SyncStep(label="Importing downloaded files").as_dict() == {
        "label": "Importing downloaded files",
        "done": 0,
        "total": 0,
    }


def test_a_step_is_frozen() -> None:
    """It is written on a worker thread and read on the event loop."""
    step = SyncStep(label="Analyzing")
    with pytest.raises(attrs.exceptions.FrozenInstanceError):
        step.label = "something else"  # type: ignore[misc]


def test_the_default_sink_accepts_what_a_sink_is_handed() -> None:
    """The port can be driven with no engine attached, which is how every ingest
    test calls it. Progress is fire-and-forget: it must never fail a sync."""
    sink: ProgressSink = no_progress
    assert sink("Downloading monitoring (452 days)", 1, 4) is None
