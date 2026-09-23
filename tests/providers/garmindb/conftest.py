"""Fixtures every GarminDB test needs.

``settings`` deliberately shadows the root fixture with a
:class:`GarminDbSettings`: everything in this package needs the corpus paths and
the Garmin knobs, and the generic ``Settings`` no longer carries either.

``corpus_settings`` and ``corpus`` were copied into eight test modules before
this file existed. They are identical in every one, and a corpus built somewhere
other than where ``settings`` looks for it is the single easiest way to write a
GarminDB test that passes for the wrong reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from garmin_health.providers.garmindb.settings import GarminDbSettings
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import Fixture
from tests.providers.garmindb.fixtures import build_fixture


@pytest.fixture
def settings(tmp_path: Path) -> GarminDbSettings:
    """Provider settings rooted in a tmpdir, so every test gets its own tree."""
    return GarminDbSettings(app_data_dir=tmp_path / "appdata")


@pytest.fixture
def corpus_settings(tmp_path: Path) -> GarminDbSettings:
    """Settings whose ``db_dir`` is where ``build_fixture`` writes its SQLite files."""
    return GarminDbSettings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


@pytest.fixture
def corpus(corpus_settings: GarminDbSettings) -> Fixture:
    """Three nights with every served metric populated."""
    return build_fixture(
        corpus_settings.health_data_dir,
        nights=3,
        heart_rate=True,
        hrv=True,
        sleep_score=82,
        resting_hr=True,
        avg_rr=14.5,
    )
