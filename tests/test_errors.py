"""The two degraded states, as exception types the HTTP layer can map.

``routes/service.py`` turns :class:`ProviderUnavailable` into a 503 and never
needs to name a provider to do it. The distinction :class:`ProviderNotReady`
draws is for ``service.py`` alone: a provider that has not finished setting
itself up serves empty when it holds nothing, and 503 once it holds data.
"""

from __future__ import annotations

import pytest

from garmin_health.errors import ProviderNotReady
from garmin_health.errors import ProviderUnavailable


def test_not_ready_is_a_kind_of_unavailable() -> None:
    """One 503 mapping covers both, so a new not-ready case cannot leak a 500."""
    assert issubclass(ProviderNotReady, ProviderUnavailable)


def test_catching_unavailable_catches_not_ready() -> None:
    with pytest.raises(ProviderUnavailable):
        raise ProviderNotReady("The account's home timezone is unknown.")


def test_the_message_survives_being_caught_as_the_base() -> None:
    """The owner-facing reason is the whole value of these; a bare type is useless."""
    with pytest.raises(ProviderUnavailable, match="GARMIN_HOME_TZ"):
        raise ProviderNotReady("The home timezone is unknown. Set GARMIN_HOME_TZ.")


def test_unavailable_is_not_mistaken_for_not_ready() -> None:
    """A stale schema is not a container that has yet to sync: it holds data and
    needs the owner, so it must never fall down the serve-empty path."""
    assert not issubclass(ProviderUnavailable, ProviderNotReady)
