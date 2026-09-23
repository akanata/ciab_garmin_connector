"""Provider packages: everything that knows how the data was acquired.

One subpackage per provider. A provider owns its acquisition strategy (a polling
scraper, a signed webhook, or a notification followed by a fetch), the store it
writes, and the reader over that store. Everything it hands upward is a
``health_data_service`` type or a stdlib type.

:func:`build_provider` is the **only** place in the app that names a concrete
provider. The core goes through it rather than importing one, which is what lets
``HEALTH_PROVIDER`` choose and what the boundary rule in ``AGENTS.md`` enforces.
"""

from __future__ import annotations

from garmin_health.config import ConfigError
from garmin_health.config import Settings
from garmin_health.ports import Provider

# Every provider this image can run. Named here rather than discovered, so an
# unknown HEALTH_PROVIDER fails at startup with the list instead of falling back
# to a default and silently acquiring nothing.
PROVIDER_NAMES = ("garmindb",)


def build_provider(settings: Settings) -> Provider:
    """Build the provider ``settings.provider`` names, rooted at the app volume."""
    if settings.provider not in PROVIDER_NAMES:
        raise ConfigError(
            f"HEALTH_PROVIDER must be one of {PROVIDER_NAMES}, got {settings.provider!r}"
        )
    # Imported here, not at module scope: importing a provider pulls in its vendor
    # libraries (GarminDB drags in SQLAlchemy and the FIT parser), and only the
    # one actually selected should pay that.
    from garmin_health.providers.garmindb.provider import GarminDbProvider  # noqa: PLC0415

    return GarminDbProvider.from_env(settings.app_data_dir)
