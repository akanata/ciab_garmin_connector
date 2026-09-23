import datetime as dt
import json
import stat
from pathlib import Path

import dateutil.parser
import pytest

from garmin_health.providers.garmindb.config_file import InvalidGarminConfig
from garmin_health.providers.garmindb.config_file import config_user
from garmin_health.providers.garmindb.config_file import ensure_config
from garmin_health.providers.garmindb.config_file import load_manager
from garmin_health.providers.garmindb.config_file import read_config
from garmin_health.providers.garmindb.config_file import render_config
from garmin_health.providers.garmindb.config_file import validate_config
from garmin_health.providers.garmindb.preferences import DOWNLOADABLE_STATS
from garmin_health.providers.garmindb.preferences import ImportPreferences
from garmin_health.providers.garmindb.preferences import save_preferences
from garmin_health.providers.garmindb.settings import GarminDbSettings


def test_rendered_config_passes_its_own_validator(settings: GarminDbSettings) -> None:
    validate_config(render_config(settings, user="rider@example.com"))


def test_directories_are_absolute_and_not_relative_to_home(settings: GarminDbSettings) -> None:
    """GarminConnectConfigManager.homedir is a CLASS attribute evaluated at import,
    so setting HOME later has no effect. An absolute base_dir is the only way to
    put the corpus under app data."""
    cfg = render_config(settings)
    assert cfg["directories"]["relative_to_home"] is False
    base = Path(cfg["directories"]["base_dir"])
    assert base.is_absolute()
    assert base == settings.health_data_dir.resolve()


def test_out_of_scope_stats_are_disabled(settings: GarminDbSettings) -> None:
    enabled = render_config(settings)["enabled_stats"]
    assert enabled["monitoring"] is True
    assert enabled["sleep"] is True
    assert enabled["rhr"] is True
    assert enabled["hrv"] is True
    # Activities are the slowest download by far and are out of scope.
    assert enabled["activities"] is False
    assert enabled["weight"] is False
    assert enabled["steps"] is False


def test_backfill_start_date_reaches_every_stat(settings: GarminDbSettings) -> None:
    tuned = GarminDbSettings(app_data_dir=settings.app_data_dir, backfill_start_date="2025-01-01")
    data = render_config(tuned)["data"]
    assert data["sleep_start_date"] == "2025-01-01"
    assert data["monitoring_start_date"] == "2025-01-01"
    assert data["rhr_start_date"] == "2025-01-01"
    assert data["hrv_start_date"] == "2025-01-01"


def test_password_is_never_rendered_into_the_config(settings: GarminDbSettings) -> None:
    """We do our own login and hand GarminDB the resulting token, so the owner's
    Garmin password never needs to touch disk at all."""
    cfg = render_config(settings, user="rider@example.com")
    assert cfg["credentials"]["user"] == "rider@example.com"
    assert cfg["credentials"]["password"] == ""
    assert cfg["credentials"]["secure_password"] is False
    assert "hunter2" not in json.dumps(cfg)


def test_every_date_key_parses_the_way_garmindb_will_parse_it(settings: GarminDbSettings) -> None:
    """JsonConfig's object_hook runs dateutil.parser.parse on every *_date key and
    an exception there reaches GarminConnectConfigManager's sys.exit(-1)."""
    for key, value in render_config(settings)["data"].items():
        if key.endswith("_date"):
            dateutil.parser.parse(value)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: c.pop("credentials"), id="missing-section"),
        pytest.param(lambda c: c.pop("directories"), id="missing-directories"),
        pytest.param(
            lambda c: c["data"].update(sleep_start_date="not-a-date"), id="unparseable-date"
        ),
        pytest.param(lambda c: c["data"].update(sleep_start_date=None), id="null-date"),
        pytest.param(lambda c: c.update(directories="nope"), id="section-not-a-mapping"),
        pytest.param(
            lambda c: c["directories"].update(base_dir="HealthData"), id="relative-base-dir"
        ),
    ],
)
def test_validator_rejects_configs_that_would_sys_exit(
    settings: GarminDbSettings, mutate: object
) -> None:
    cfg = render_config(settings)
    mutate(cfg)  # type: ignore[operator]
    with pytest.raises(InvalidGarminConfig):
        validate_config(cfg)


def test_validator_rejects_a_non_mapping_document() -> None:
    with pytest.raises(InvalidGarminConfig):
        validate_config([1, 2, 3])


def test_ensure_config_writes_owner_only_permissions(settings: GarminDbSettings) -> None:
    path = ensure_config(settings, user="rider@example.com")
    assert path == settings.garmin_config_file
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ensure_config_preserves_an_existing_user_when_not_given_one(
    settings: GarminDbSettings,
) -> None:
    ensure_config(settings, user="rider@example.com")
    ensure_config(settings)
    assert config_user(settings) == "rider@example.com"


def test_ensure_config_repairs_a_corrupt_file(settings: GarminDbSettings) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.garmin_config_file.write_text("{ this is not json")
    ensure_config(settings, user="rider@example.com")
    validate_config(json.loads(settings.garmin_config_file.read_text()))


def test_load_manager_returns_a_usable_manager(settings: GarminDbSettings) -> None:
    ensure_config(settings, user="rider@example.com")
    manager = load_manager(settings)
    assert manager.get_user() == "rider@example.com"
    assert Path(manager.get_token_store_file()) == settings.token_file


def test_load_manager_raises_instead_of_exiting_on_a_bad_config(settings: GarminDbSettings) -> None:
    """The bare GarminConnectConfigManager would call sys.exit(-1) here, which in a
    server process is an unrecoverable exit rather than a handleable error."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.garmin_config_file.write_text("{ not json")
    with pytest.raises(InvalidGarminConfig):
        load_manager(settings, repair=False)


class TestImportScope:
    """The owner's saved scope is what reaches GarminConnectConfig.json."""

    def test_the_saved_start_date_is_written_to_every_stat(
        self, settings: GarminDbSettings
    ) -> None:
        prefs = ImportPreferences(
            start_date=dt.date(2024, 3, 1), enabled_stats=frozenset(DOWNLOADABLE_STATS)
        )
        document = render_config(settings, preferences=prefs)
        dates = {k: v for k, v in document["data"].items() if k.endswith("_start_date")}
        assert set(dates.values()) == {"2024-03-01"}

    def test_disabled_metrics_are_switched_off_in_the_config(
        self, settings: GarminDbSettings
    ) -> None:
        prefs = ImportPreferences(
            start_date=dt.date(2024, 3, 1), enabled_stats=frozenset({"sleep", "hrv"})
        )
        stats = render_config(settings, preferences=prefs)["enabled_stats"]
        assert stats["sleep"] is True
        assert stats["hrv"] is True
        assert stats["monitoring"] is False
        assert stats["rhr"] is False

    def test_the_stats_we_never_support_stay_off_whatever_is_saved(
        self, settings: GarminDbSettings
    ) -> None:
        """activities is by far the slowest download and has no download branch."""
        prefs = ImportPreferences(
            start_date=dt.date(2024, 3, 1), enabled_stats=frozenset(DOWNLOADABLE_STATS)
        )
        stats = render_config(settings, preferences=prefs)["enabled_stats"]
        assert stats["activities"] is False
        assert stats["weight"] is False

    def test_every_key_garmindb_knows_is_still_present(self, settings: GarminDbSettings) -> None:
        """Statistics.from_string runs over whatever keys are here, and an absent
        enabled_stats block makes GarminDB default every statistic to True."""
        prefs = ImportPreferences(start_date=dt.date(2024, 3, 1), enabled_stats=frozenset())
        stats = render_config(settings, preferences=prefs)["enabled_stats"]
        assert set(stats) == {
            "monitoring",
            "steps",
            "itime",
            "sleep",
            "rhr",
            "weight",
            "activities",
            "hrv",
        }
        assert not any(stats.values())

    def test_an_empty_selection_is_a_valid_config(self, settings: GarminDbSettings) -> None:
        prefs = ImportPreferences(start_date=dt.date(2024, 3, 1), enabled_stats=frozenset())
        validate_config(render_config(settings, preferences=prefs))

    def test_the_manager_reports_exactly_what_was_enabled(self, settings: GarminDbSettings) -> None:
        """End to end through GarminDB's own reader, which is what ingest calls."""
        save_preferences(
            settings,
            ImportPreferences(
                start_date=dt.date(2024, 3, 1), enabled_stats=frozenset({"sleep", "rhr"})
            ),
        )
        manager = load_manager(settings)
        assert {s.name for s in manager.enabled_stats()} == {"sleep", "rhr"}
        assert manager.stat_start_date("sleep")[0] == dt.date(2024, 3, 1)

    def test_ensure_config_reads_the_saved_scope_by_default(
        self, settings: GarminDbSettings
    ) -> None:
        save_preferences(
            settings,
            ImportPreferences(start_date=dt.date(2025, 6, 1), enabled_stats=frozenset({"hrv"})),
        )
        ensure_config(settings)
        raw = read_config(settings)
        assert raw["data"]["sleep_start_date"] == "2025-06-01"
        assert raw["enabled_stats"]["hrv"] is True
        assert raw["enabled_stats"]["sleep"] is False

    def test_saving_the_scope_does_not_lose_the_linked_account(
        self, settings: GarminDbSettings
    ) -> None:
        ensure_config(settings, user="rider@example.com")
        save_preferences(
            settings,
            ImportPreferences(start_date=dt.date(2025, 6, 1), enabled_stats=frozenset({"hrv"})),
        )
        ensure_config(settings)
        assert read_config(settings)["credentials"]["user"] == "rider@example.com"
