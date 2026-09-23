"""What a running acquisition says about itself while it runs.

Generic because ``/setup`` is generic: whatever a provider is doing -- downloading
days from Garmin, draining a queue of webhook payloads -- it reports a label and
an optional count, and the page's polling script reads exactly those three keys
off ``/sync/status``. Renaming any of them silently empties the progress line.

The sink is **fire-and-forget by design**. It is handed across a thread boundary
to code that is in the middle of real work, so it must never be the thing that
fails an otherwise complete run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import attrs

ProgressSink = Callable[[str, int, int], None]
"""``(what is happening now, steps finished, steps total)``. ``total`` 0 = unknown."""


def no_progress(label: str, done: int, total: int) -> None:
    """Default sink, so a port can be driven without an engine attached."""


@attrs.frozen
class SyncStep:
    """What the worker thread is doing right now.

    Frozen because it is written on that worker thread and read on the event
    loop; replacing the whole value is what makes that safe.
    """

    label: str
    done: int = 0
    total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"label": self.label, "done": self.done, "total": self.total}
