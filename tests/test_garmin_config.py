import json
import stat
from pathlib import Path

import dateutil.parser
import pytest

from garmin_health.config import Settings
from garmin_health.garmin_config import InvalidGarminConfig
from garmin_health.garmin_config import config_user
from garmin_health.garmin_config import ensure_config
from garmin_health.garmin_config import load_manager
from garmin_health.garmin_config import render_config
from garmin_health.garmin_config import validate_config


def test_rendered_config_passes_its_own_validator(settings: Settings) -> None:
    validate_config(render_config(settings, user="rider@example.com"))


def test_directories_are_absolute_and_not_relative_to_home(settings: Settings) -> None:
    """GarminConnectConfigManager.homedir is a CLASS attribute evaluated at import,
    so setting HOME later has no effect. An absolute base_dir is the only way to
    put the corpus under app data."""
    cfg = render_config(settings)
    assert cfg["directories"]["relative_to_home"] is False
    base = Path(cfg["directories"]["base_dir"])
    assert base.is_absolute()
    assert base == settings.health_data_dir.resolve()


def test_out_of_scope_stats_are_disabled(settings: Settings) -> None:
    enabled = render_config(settings)["enabled_stats"]
    assert enabled["monitoring"] is True
    assert enabled["sleep"] is True
    assert enabled["rhr"] is True
    assert enabled["hrv"] is True
    # Activities are the slowest download by far and are out of scope.
    assert enabled["activities"] is False
    assert enabled["weight"] is False
    assert enabled["steps"] is False


def test_backfill_start_date_reaches_every_stat(settings: Settings) -> None:
    tuned = Settings(app_data_dir=settings.app_data_dir, backfill_start_date="2025-01-01")
    data = render_config(tuned)["data"]
    assert data["sleep_start_date"] == "2025-01-01"
    assert data["monitoring_start_date"] == "2025-01-01"
    assert data["rhr_start_date"] == "2025-01-01"
    assert data["hrv_start_date"] == "2025-01-01"


def test_password_is_never_rendered_into_the_config(settings: Settings) -> None:
    """We do our own login and hand GarminDB the resulting token, so the owner's
    Garmin password never needs to touch disk at all."""
    cfg = render_config(settings, user="rider@example.com")
    assert cfg["credentials"]["user"] == "rider@example.com"
    assert cfg["credentials"]["password"] == ""
    assert cfg["credentials"]["secure_password"] is False
    assert "hunter2" not in json.dumps(cfg)


def test_every_date_key_parses_the_way_garmindb_will_parse_it(settings: Settings) -> None:
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
def test_validator_rejects_configs_that_would_sys_exit(settings: Settings, mutate: object) -> None:
    cfg = render_config(settings)
    mutate(cfg)  # type: ignore[operator]
    with pytest.raises(InvalidGarminConfig):
        validate_config(cfg)


def test_validator_rejects_a_non_mapping_document() -> None:
    with pytest.raises(InvalidGarminConfig):
        validate_config([1, 2, 3])


def test_ensure_config_writes_owner_only_permissions(settings: Settings) -> None:
    path = ensure_config(settings, user="rider@example.com")
    assert path == settings.garmin_config_file
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ensure_config_preserves_an_existing_user_when_not_given_one(settings: Settings) -> None:
    ensure_config(settings, user="rider@example.com")
    ensure_config(settings)
    assert config_user(settings) == "rider@example.com"


def test_ensure_config_repairs_a_corrupt_file(settings: Settings) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.garmin_config_file.write_text("{ this is not json")
    ensure_config(settings, user="rider@example.com")
    validate_config(json.loads(settings.garmin_config_file.read_text()))


def test_load_manager_returns_a_usable_manager(settings: Settings) -> None:
    ensure_config(settings, user="rider@example.com")
    manager = load_manager(settings)
    assert manager.get_user() == "rider@example.com"
    assert Path(manager.get_token_store_file()) == settings.token_file


def test_load_manager_raises_instead_of_exiting_on_a_bad_config(settings: Settings) -> None:
    """The bare GarminConnectConfigManager would call sys.exit(-1) here, which in a
    server process is an unrecoverable exit rather than a handleable error."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.garmin_config_file.write_text("{ not json")
    with pytest.raises(InvalidGarminConfig):
        load_manager(settings, repair=False)
