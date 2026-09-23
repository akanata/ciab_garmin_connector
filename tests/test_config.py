"""The generic settings: the volume, the provider name, and the serving limits.

Everything GarminDB used to keep here -- the domain, the timezones, the download
floor, the paths -- moved to ``tests/providers/garmindb/test_settings.py`` with
the fields themselves.
"""

from __future__ import annotations

from pathlib import Path

import attrs
import pytest

from garmin_health.config import DEFAULT_LIMIT
from garmin_health.config import DEFAULT_PROVIDER
from garmin_health.config import MAX_LIMIT
from garmin_health.config import MAX_ROWS_SCANNED
from garmin_health.config import MAX_SESSION_SUBSERIES
from garmin_health.config import ConfigError
from garmin_health.config import Settings
from garmin_health.config import flag_from
from garmin_health.config import settings_from_env


class TestAppDataDir:
    def test_prefers_bottle_app_data_dir_over_openhost(self) -> None:
        s = settings_from_env(
            {"BOTTLE_APP_DATA_DIR": "/bottle", "OPENHOST_APP_DATA_DIR": "/openhost"}
        )
        assert s.app_data_dir == Path("/bottle")

    def test_falls_back_to_openhost_app_data_dir(self) -> None:
        """Routers from before 2026-08-27 export only OPENHOST_APP_DATA_DIR."""
        s = settings_from_env({"OPENHOST_APP_DATA_DIR": "/openhost"})
        assert s.app_data_dir == Path("/openhost")

    def test_defaults_when_no_app_data_dir_is_exported(self) -> None:
        assert settings_from_env({}).app_data_dir == Path("data")


class TestProviderSelection:
    def test_it_defaults_to_garmindb(self) -> None:
        """The only provider there is. Naming it in the environment rather than
        assuming it is what lets a second one be added without a fork."""
        assert settings_from_env({}).provider == DEFAULT_PROVIDER == "garmindb"

    def test_it_is_read_from_the_environment(self) -> None:
        assert settings_from_env({"HEALTH_PROVIDER": "garmindb"}).provider == "garmindb"

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_an_unset_value_means_the_default(self, raw: str) -> None:
        """Compose and the router both export empty strings for unset variables."""
        assert settings_from_env({"HEALTH_PROVIDER": raw}).provider == "garmindb"

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert settings_from_env({"HEALTH_PROVIDER": " garmindb "}).provider == "garmindb"


def test_the_generic_settings_know_nothing_about_any_provider() -> None:
    """The whole point of the split. A Garmin domain or a home timezone appearing
    here again would mean the core had started caring which provider it runs."""
    assert {f.name for f in attrs.fields(Settings)} == {"app_data_dir", "provider"}


def test_serving_limits_have_the_documented_defaults() -> None:
    assert (DEFAULT_LIMIT, MAX_LIMIT, MAX_ROWS_SCANNED) == (5_000, 50_000, 1_000_000)
    assert MAX_SESSION_SUBSERIES == 2_000


class TestFlagParsing:
    """Shared with every provider, so it stays in the generic config module."""

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "on"])
    def test_truthy_spellings(self, raw: str) -> None:
        assert flag_from({"FLAG": raw}, "FLAG") is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off", ""])
    def test_falsy_spellings(self, raw: str) -> None:
        assert flag_from({"FLAG": raw}, "FLAG") is False

    def test_an_unset_flag_is_false(self) -> None:
        assert flag_from({}, "FLAG") is False

    def test_a_nonsense_value_fails_loudly(self) -> None:
        """Silently reading "maybe" as off would leave the owner believing they
        had switched something on."""
        with pytest.raises(ConfigError):
            flag_from({"FLAG": "maybe"}, "FLAG")


DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
ROUTER_VOLUME = "/data/app_data/garmin-connector"


def image_env() -> dict[str, str]:
    """The ENV the image bakes in. ``podman run -e`` then overrides it key by key."""
    env: dict[str, str] = {}
    for line in DOCKERFILE.read_text().replace("\\\n", " ").splitlines():
        parts = line.split()
        if not parts or parts[0].upper() != "ENV":
            continue
        if len(parts) > 1 and "=" not in parts[1]:
            env[parts[1]] = " ".join(parts[2:])  # the legacy `ENV KEY value` form
            continue
        for pair in parts[1:]:
            key, _, value = pair.partition("=")
            env[key] = value
    return env


def test_the_dockerfile_env_parser_sees_the_images_env() -> None:
    """Guards the test below from passing vacuously on a parser that finds nothing."""
    assert image_env().get("TZ") == "UTC"


@pytest.mark.parametrize(
    "router_env",
    [
        pytest.param({"OPENHOST_APP_DATA_DIR": ROUTER_VOLUME}, id="router-before-2026-08-27"),
        pytest.param(
            {"OPENHOST_APP_DATA_DIR": ROUTER_VOLUME, "BOTTLE_APP_DATA_DIR": ROUTER_VOLUME},
            id="router-exporting-both-names",
        ),
    ],
)
def test_the_image_never_shadows_the_routers_volume(router_env: dict[str, str]) -> None:
    """Anything written outside the router's mount is lost on every update.

    The router ``podman rm -f``s the container on each update, reload and restart.
    A router from before 2026-08-27 exports only OPENHOST_APP_DATA_DIR, so a
    BOTTLE_APP_DATA_DIR baked into the image wins the name precedence and puts the
    token, the preferences and the whole corpus on the container's own disk.
    """
    s = settings_from_env({**image_env(), **router_env})
    assert s.app_data_dir == Path(ROUTER_VOLUME)
