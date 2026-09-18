from collections.abc import Iterator
from pathlib import Path

import pytest

from garmin_health.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings rooted in a tmpdir, so every test gets its own app-data tree."""
    return Settings(app_data_dir=tmp_path / "appdata")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient BOTTLE_/OPENHOST_ vars leaking in from the developer's shell."""
    for name in (
        "BOTTLE_APP_DATA_DIR",
        "OPENHOST_APP_DATA_DIR",
        "HEALTH_PROVIDER",
        "GARMIN_HOME_TZ",
        "GARMIN_IMPORT_TZ",
        "GARMIN_BACKFILL_START_DATE",
        "GARMIN_DOMAIN",
        "GARMIN_FILL_STAGE_GAPS",
        "GARMIN_DERIVE_RESTLESS_PERIODS",
        "SYNC_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
