"""``GarminDbSettings``: the environment this provider reads, and where it writes.

These moved out of ``tests/test_config.py`` with the fields themselves. The path
tests matter most: they are the guarantee that splitting the settings object
changed **no on-disk location**, so an existing deployment keeps its token, its
preferences and its corpus without migrating anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from garmindb import GarminConnectConfigManager

from garmin_health.config import ConfigError
from garmin_health.providers.garmindb.config_file import ensure_config
from garmin_health.providers.garmindb.settings import GarminDbSettings


def from_env(env: dict[str, str], app_data_dir: str = "/data") -> GarminDbSettings:
    return GarminDbSettings.from_env(Path(app_data_dir), env)


class TestOnDiskLayout:
    """Every path below is where a deployed container already has files."""

    def test_derived_paths_all_live_under_app_data(self, settings: GarminDbSettings) -> None:
        root = settings.app_data_dir
        assert settings.config_dir == root / "GarminDb"
        assert settings.garmin_config_file == root / "GarminDb" / "GarminConnectConfig.json"
        assert settings.token_file == root / "GarminDb" / "garmin_tokens.json"
        assert settings.health_data_dir == root / "HealthData"

    def test_the_sqlite_files_are_where_garmindb_puts_them(
        self, settings: GarminDbSettings
    ) -> None:
        """GarminConnectConfigManager.get_db_dir() is <base_dir>/DBs. The serving
        side derives it directly so it never has to render or read the config."""
        assert settings.db_dir == settings.health_data_dir / "DBs"

    def test_the_sync_state_lives_under_app_data(self, settings: GarminDbSettings) -> None:
        """When the last sync started has to survive a restart. Without it every
        deploy waits out a whole interval, and a crash loop would sign in every time."""
        assert settings.sync_state_file == settings.app_data_dir / "sync_state.json"

    def test_the_preferences_live_under_app_data(self, settings: GarminDbSettings) -> None:
        assert settings.preferences_file == settings.app_data_dir / "import_preferences.json"

    def test_token_file_matches_garmindb_expectation(self, settings: GarminDbSettings) -> None:
        """GarminConnectConfigManager.get_token_store_file() is
        <config_dir>/garmin_tokens.json.

        If these ever diverge, our login would write a token GarminDB never reads
        and every sync would fall back to a credential login that has no password
        on disk.
        """
        ensure_config(settings, user="rider@example.com")
        manager = GarminConnectConfigManager(str(settings.config_dir))
        assert Path(manager.get_token_store_file()) == settings.token_file


class TestFromEnv:
    def test_the_app_data_dir_is_given_rather_than_read(self) -> None:
        """The volume is the platform's business, not this provider's: the generic
        Settings resolves it and hands it down."""
        assert from_env({}, app_data_dir="/data/app_data/x").app_data_dir == Path(
            "/data/app_data/x"
        )

    def test_the_domain_defaults_and_is_validated(self) -> None:
        assert from_env({}).garmin_domain == "garmin.com"
        assert from_env({"GARMIN_DOMAIN": "garmin.cn"}).is_cn is True
        assert from_env({}).is_cn is False

    def test_an_unsupported_domain_fails_loudly(self) -> None:
        with pytest.raises(ConfigError):
            from_env({"GARMIN_DOMAIN": "garmin.example"})

    def test_home_tz_is_validated_as_a_real_zone(self) -> None:
        assert from_env({"GARMIN_HOME_TZ": "America/Denver"}).home_tz == "America/Denver"

    def test_bogus_home_tz_fails_at_startup_rather_than_silently(self) -> None:
        """A wrong timezone corrupts every emitted timestamp and would not be
        noticed for months, so an unresolvable zone must fail loudly here."""
        with pytest.raises(ConfigError):
            from_env({"GARMIN_HOME_TZ": "Mars/Olympus_Mons"})

    def test_import_tz_is_read_and_validated(self) -> None:
        """Names the TZ the GarminDB corpus was imported under, when it was not the
        home zone. Exact, unlike the offset learned from the data."""
        assert from_env({"GARMIN_IMPORT_TZ": "UTC"}).import_tz == "UTC"
        assert from_env({}).import_tz is None

    def test_bogus_import_tz_fails_at_startup(self) -> None:
        with pytest.raises(ConfigError):
            from_env({"GARMIN_IMPORT_TZ": "Mars/Olympus_Mons"})

    def test_backfill_start_date_defaults_to_garmindbs_example(self) -> None:
        assert from_env({}).backfill_start_date == "2019-12-31"

    def test_backfill_start_date_is_read_from_env(self) -> None:
        """A full backfill is ~1s per day per stat, so 2019 means hours on first
        run. The owner needs to shorten it without editing JSON in a container."""
        assert from_env({"GARMIN_BACKFILL_START_DATE": "2025-01-01"}).backfill_start_date == (
            "2025-01-01"
        )

    def test_bogus_backfill_start_date_fails_at_startup(self) -> None:
        """An unparseable date reaches GarminConnectConfigManager's sys.exit(-1)."""
        with pytest.raises(ConfigError):
            from_env({"GARMIN_BACKFILL_START_DATE": "last tuesday-ish"})

    def test_sync_interval_defaults_to_one_hour(self) -> None:
        """Freshness is capped by how often the watch syncs to Garmin Connect
        through the phone, so much shorter buys little -- but six hours left last
        night's sleep missing for most of a morning."""
        assert from_env({}).sync_interval_seconds == 3600

    def test_sync_interval_is_read_from_env(self) -> None:
        assert from_env({"SYNC_INTERVAL_SECONDS": "900"}).sync_interval_seconds == 900

    @pytest.mark.parametrize("bad", ["0", "-1", "abc"])
    def test_bad_sync_interval_fails_loudly(self, bad: str) -> None:
        with pytest.raises(ConfigError):
            from_env({"SYNC_INTERVAL_SECONDS": bad})

    def test_empty_sync_interval_means_unset(self) -> None:
        """Compose and the router both export empty strings for unset variables."""
        assert from_env({"SYNC_INTERVAL_SECONDS": ""}).sync_interval_seconds == 3600


class TestInventionFlags:
    """Both default off because both make the service emit data Garmin never
    recorded."""

    def test_stage_gap_filling_is_off_by_default(self) -> None:
        """A gap means Garmin recorded nothing; UNKNOWN filler would be invented."""
        assert from_env({}).fill_stage_gaps is False
        assert from_env({"GARMIN_FILL_STAGE_GAPS": "true"}).fill_stage_gaps is True

    def test_restless_period_derivation_is_off_by_default(self) -> None:
        """Oura's restless_periods is movement-derived, not an awakening count.
        Serving a Garmin awakening count under that id would corrupt a merged list."""
        assert from_env({}).derive_restless_periods is False
        assert from_env({"GARMIN_DERIVE_RESTLESS_PERIODS": "1"}).derive_restless_periods is True

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_flag_spellings_are_all_accepted(self, raw: str) -> None:
        assert from_env({"GARMIN_FILL_STAGE_GAPS": raw}).fill_stage_gaps is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off", ""])
    def test_falsy_flag_spellings_are_all_rejected(self, raw: str) -> None:
        assert from_env({"GARMIN_FILL_STAGE_GAPS": raw}).fill_stage_gaps is False

    def test_a_nonsense_flag_value_fails_loudly(self) -> None:
        """Silently reading "maybe" as off would leave the owner believing they
        had switched something on."""
        with pytest.raises(ConfigError):
            from_env({"GARMIN_FILL_STAGE_GAPS": "maybe"})
