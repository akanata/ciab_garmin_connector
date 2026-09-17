"""The two ways a provider can fail to answer, as types the HTTP layer maps.

``routes/service.py`` turns :class:`ProviderUnavailable` into a 503 without ever
naming a provider. That is what lets a provider define its own richer failures --
a stale SQLite schema here, a webhook secret that was never configured in some
later one -- and still reach a consumer as a 503 rather than a 500.

The distinction :class:`ProviderNotReady` draws belongs to ``service.py`` alone,
and the two must not be conflated. A provider that has not finished setting
itself up serves an empty 200 while it holds nothing, because a brand-new
install is not broken; it serves 503 once it holds data it cannot place on a
real clock, because answering with those rows on the wrong clock would shift
every timestamp by hours with nothing raising anywhere.
"""

from __future__ import annotations


class ProviderUnavailable(Exception):
    """The provider cannot serve right now. Routes turn this into a 503."""


class ProviderNotReady(ProviderUnavailable):
    """The provider has not finished setting itself up.

    Survivable, unlike the general case: a container that has never acquired
    anything is new rather than faulty, and the honest answer for it is empty.
    """
