from pathlib import Path

import pytest
from garmindb import GarminConnectConfigManager

from garmin_health.config import ConfigError
from garmin_health.config import Settings
from garmin_health.config import settings_from_env
from garmin_health.garmin_config import ensure_config


def test_prefers_bottle_app_data_dir_over_openhost() -> None:
    s = settings_from_env({"BOTTLE_APP_DATA_DIR": "/bottle", "OPENHOST_APP_DATA_DIR": "/openhost"})
    assert s.app_data_dir == Path("/bottle")


def test_falls_back_to_openhost_app_data_dir() -> None:
    """Older deployments (and fitpub_oh) still export only OPENHOST_APP_DATA_DIR."""
    s = settings_from_env({"OPENHOST_APP_DATA_DIR": "/openhost"})
    assert s.app_data_dir == Path("/openhost")


def test_defaults_when_no_app_data_dir_is_exported() -> None:
    s = settings_from_env({})
    assert s.app_data_dir == Path("data")


def test_derived_paths_all_live_under_app_data(settings: Settings) -> None:
    root = settings.app_data_dir
    assert settings.config_dir == root / "GarminDb"
    assert settings.garmin_config_file == root / "GarminDb" / "GarminConnectConfig.json"
    assert settings.token_file == root / "GarminDb" / "garmin_tokens.json"
    assert settings.health_data_dir == root / "HealthData"


def test_token_file_matches_garmindb_expectation(settings: Settings) -> None:
    """GarminConnectConfigManager.get_token_store_file() is <config_dir>/garmin_tokens.json.

    If these ever diverge, our login would write a token GarminDB never reads and
    every sync would fall back to a credential login that has no password on disk.
    """
    ensure_config(settings, user="rider@example.com")
    manager = GarminConnectConfigManager(str(settings.config_dir))
    assert Path(manager.get_token_store_file()) == settings.token_file


def test_is_cn_follows_domain() -> None:
    assert settings_from_env({"GARMIN_DOMAIN": "garmin.cn"}).is_cn is True
    assert settings_from_env({}).is_cn is False


def test_sync_interval_defaults_to_six_hours() -> None:
    assert settings_from_env({}).sync_interval_seconds == 21600


def test_sync_interval_is_read_from_env() -> None:
    assert settings_from_env({"SYNC_INTERVAL_SECONDS": "900"}).sync_interval_seconds == 900


@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
def test_bad_sync_interval_fails_loudly(bad: str) -> None:
    with pytest.raises(ConfigError):
        settings_from_env({"SYNC_INTERVAL_SECONDS": bad})


def test_empty_sync_interval_means_unset() -> None:
    """Compose and the router both export empty strings for unset variables."""
    assert settings_from_env({"SYNC_INTERVAL_SECONDS": ""}).sync_interval_seconds == 21600


def test_home_tz_is_validated_as_a_real_zone() -> None:
    assert settings_from_env({"GARMIN_HOME_TZ": "America/Denver"}).home_tz == "America/Denver"


def test_import_tz_is_read_and_validated() -> None:
    """Names the TZ the GarminDB corpus was imported under, when it was not the
    home zone. Exact, unlike the offset learned from the data."""
    assert settings_from_env({"GARMIN_IMPORT_TZ": "UTC"}).import_tz == "UTC"
    assert settings_from_env({}).import_tz is None


def test_bogus_import_tz_fails_at_startup() -> None:
    with pytest.raises(ConfigError):
        settings_from_env({"GARMIN_IMPORT_TZ": "Mars/Olympus_Mons"})


def test_bogus_home_tz_fails_at_startup_rather_than_silently() -> None:
    """A wrong timezone corrupts every emitted timestamp and would not be noticed
    for months, so an unresolvable zone must fail loudly here."""
    with pytest.raises(ConfigError):
        settings_from_env({"GARMIN_HOME_TZ": "Mars/Olympus_Mons"})
