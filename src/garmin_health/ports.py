"""The ports a provider implements, and the vocabulary the core speaks.

A provider owns how data is acquired -- a polling scraper, a signed webhook, a
notification followed by a fetch -- along with the store it writes and the reader
over that store. What crosses this boundary is a ``health_data_service`` type, a
:class:`~garmin_health.registry.MetricEntry`, or a stdlib type. Never a database
handle, a session, or a vendor client.

``MetricEntry`` is imported only for type checking: ``registry.py`` imports
:data:`SOURCE` from here at runtime, so importing it back would be a cycle.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import TYPE_CHECKING
from typing import Protocol
from typing import runtime_checkable

from health_data_service import SleepSession

if TYPE_CHECKING:
    from garmin_health.registry import MetricEntry

# The device the data came off, which is Garmin whichever way it reached us --
# scraped from Garmin Connect, or relayed by an aggregator that read the same
# watch. A consumer merging across providers keys on this, so it is one constant
# rather than a per-provider attribute that could quietly diverge.
SOURCE = "garmin"


@runtime_checkable
class HealthReader(Protocol):
    """Read access to whatever one provider has acquired.

    Deliberately small. There is no ``open`` (a provider hands back an already
    open reader) and no ``reset`` (pooled handles are the provider's problem, and
    a webhook provider has none). ``close`` is here only because the app's
    lifespan owns the reader's lifetime.
    """

    @property
    def fault(self) -> str | None:
        """Why this provider cannot serve, in words for the owner, or None."""

    @property
    def metrics(self) -> Mapping[str, MetricEntry]:
        """The catalog this provider contributes, already bound to its store."""

    def has_any_data(self) -> bool:
        """Whether anything at all has been acquired.

        Consulted only when the provider is not ready, to tell a container that
        has never acquired anything (serve empty, 200) from one holding data it
        cannot place on a real clock (503).
        """

    def sleep_sessions(
        self, start: dt.datetime | None, end: dt.datetime | None, limit: int
    ) -> list[SleepSession]:
        """Sessions starting in ``[start, end)``, newest first.

        ``limit`` has to bound the **work**, not just the answer: the spec's
        client sends a limit and no window, so an unbounded request over the
        whole store is the normal case.
        """

    def close(self) -> None: ...
