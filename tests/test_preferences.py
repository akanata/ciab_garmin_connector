"""Owner-editable import scope, persisted to app data."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import dateutil.parser
import pytest

from garmin_health.config import Settings
from garmin_health.preferences import DOWNLOADABLE_STATS
from garmin_health.preferences import ImportPreferences
from garmin_health.preferences import InvalidPreferences
from garmin_health.preferences import load_preferences
from garmin_health.preferences import parse_preferences
from garmin_health.preferences import save_preferences

TODAY = dt.date(2026, 9, 10)


class TestDefaults:
    def test_the_env_var_is_the_default_start_date(self, settings: Settings) -> None:
        """GARMIN_BACKFILL_START_DATE seeds the first run; once the owner saves a
        preference the file wins, because otherwise editing the page would appear
        to work and then silently revert on the next restart."""
        assert load_preferences(settings).start_date == dt.date(2019, 12, 31)

    def test_an_overridden_env_var_is_honoured(self, tmp_path: Path) -> None:
        settings = Settings(app_data_dir=tmp_path / "a", backfill_start_date="2025-01-01")
        assert load_preferences(settings).start_date == dt.date(2025, 1, 1)

    def test_every_downloadable_stat_is_on_by_default(self, settings: Settings) -> None:
        assert load_preferences(settings).enabled_stats == frozenset(DOWNLOADABLE_STATS)

    def test_only_stats_the_downloader_actually_handles_are_offered(self) -> None:
        """GarminDB knows eight statistics; we implement download branches for
        four. Offering the others would be a checkbox that does nothing."""
        assert DOWNLOADABLE_STATS == ("monitoring", "sleep", "rhr", "hrv")


class TestRoundTrip:
    def test_saved_preferences_are_read_back(self, settings: Settings) -> None:
        prefs = ImportPreferences(
            start_date=dt.date(2024, 3, 1), enabled_stats=frozenset({"sleep", "hrv"})
        )
        save_preferences(settings, prefs)
        assert load_preferences(settings) == prefs

    def test_the_file_lands_under_app_data(self, settings: Settings) -> None:
        """It has to survive a container restart, or the owner re-enters it forever."""
        save_preferences(settings, load_preferences(settings))
        assert settings.preferences_file.exists()
        assert settings.app_data_dir in settings.preferences_file.parents

    def test_it_is_written_atomically(self, settings: Settings) -> None:
        save_preferences(settings, load_preferences(settings))
        assert list(settings.app_data_dir.glob("*.tmp")) == []

    def test_the_stored_document_is_readable_json(self, settings: Settings) -> None:
        save_preferences(
            settings,
            ImportPreferences(start_date=dt.date(2024, 3, 1), enabled_stats=frozenset({"sleep"})),
        )
        raw = json.loads(settings.preferences_file.read_text())
        assert raw["start_date"] == "2024-03-01"
        assert raw["enabled_stats"] == ["sleep"]


class TestCorruptFile:
    def test_unparseable_json_falls_back_to_the_defaults(
        self, settings: Settings, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A broken preferences file must not take out /setup, which is the only
        place the owner could fix it."""
        settings.preferences_file.parent.mkdir(parents=True, exist_ok=True)
        settings.preferences_file.write_text("{not json")
        assert load_preferences(settings).start_date == dt.date(2019, 12, 31)
        assert "preferences" in caplog.text.lower()

    def test_an_unknown_stat_in_the_file_is_dropped(self, settings: Settings) -> None:
        """A downgrade, or a hand-edit. Statistics.from_string would raise inside
        GarminDB if we passed it straight through."""
        settings.preferences_file.parent.mkdir(parents=True, exist_ok=True)
        settings.preferences_file.write_text(
            json.dumps({"start_date": "2024-01-01", "enabled_stats": ["sleep", "telepathy"]})
        )
        assert load_preferences(settings).enabled_stats == frozenset({"sleep"})

    def test_a_bad_date_in_the_file_falls_back_to_the_default(self, settings: Settings) -> None:
        settings.preferences_file.parent.mkdir(parents=True, exist_ok=True)
        settings.preferences_file.write_text(
            json.dumps({"start_date": "whenever", "enabled_stats": ["sleep"]})
        )
        loaded = load_preferences(settings)
        assert loaded.start_date == dt.date(2019, 12, 31)
        assert loaded.enabled_stats == frozenset({"sleep"})


class TestParsingFormInput:
    def test_it_accepts_what_a_date_input_submits(self, settings: Settings) -> None:
        prefs = parse_preferences(
            settings, start_date="2024-03-01", stats=["sleep", "hrv"], today=TODAY
        )
        assert prefs.start_date == dt.date(2024, 3, 1)
        assert prefs.enabled_stats == frozenset({"sleep", "hrv"})

    def test_an_empty_selection_means_pause_imports(self, settings: Settings) -> None:
        """A legitimate state: stop downloading without unlinking the account."""
        assert parse_preferences(settings, start_date="2024-03-01", stats=[], today=TODAY)

    @pytest.mark.parametrize("bad", ["", "whenever", "2024-13-45", "03/01/2024"])
    def test_an_unparseable_date_is_refused(self, settings: Settings, bad: str) -> None:
        with pytest.raises(InvalidPreferences):
            parse_preferences(settings, start_date=bad, stats=["sleep"], today=TODAY)

    def test_a_future_date_is_refused(self, settings: Settings) -> None:
        """It would ask Garmin for a negative span and download nothing, forever."""
        with pytest.raises(InvalidPreferences, match="future"):
            parse_preferences(settings, start_date="2027-01-01", stats=["sleep"], today=TODAY)

    def test_today_itself_is_allowed(self, settings: Settings) -> None:
        assert (
            parse_preferences(
                settings, start_date=TODAY.isoformat(), stats=["sleep"], today=TODAY
            ).start_date
            == TODAY
        )

    def test_an_unknown_stat_is_refused_rather_than_ignored(self, settings: Settings) -> None:
        """Silently dropping it would leave the owner believing they enabled it."""
        with pytest.raises(InvalidPreferences, match="telepathy"):
            parse_preferences(settings, start_date="2024-03-01", stats=["telepathy"], today=TODAY)


def test_preferences_render_the_garmindb_date_format(settings: Settings) -> None:
    """GarminConnectConfigManager runs dateutil.parser.parse on any *_date key and
    reaches sys.exit(-1) if it fails."""
    prefs = ImportPreferences(start_date=dt.date(2024, 3, 1), enabled_stats=frozenset())
    assert dateutil.parser.parse(prefs.start_date_text).date() == dt.date(2024, 3, 1)
