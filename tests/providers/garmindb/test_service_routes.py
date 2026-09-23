"""``/api/v1/*`` over the real app and a real corpus.

The status codes and envelopes are pinned provider-free in
``tests/test_service_routes.py``. What is here needs GarminDB: real sample
counts, the scan cap over real rows, the stage timeline a night actually has,
and the rebuild path out of a stale schema.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from health_data_service import SleepSession
from health_data_service.client import converter as consumer_converter
from litestar.testing import TestClient

from garmin_health.providers.garmindb.settings import GarminDbSettings
from tests.providers.garmindb.fakes import client_for
from tests.providers.garmindb.fakes import make_provider
from tests.providers.garmindb.fixtures import Fixture
from tests.providers.garmindb.fixtures import build_fixture

OWNER = {"X-OpenHost-Is-Owner": "true"}


@pytest.fixture
def corpus(corpus_settings: GarminDbSettings) -> Fixture:
    return build_fixture(
        corpus_settings.health_data_dir,
        nights=3,
        heart_rate=True,
        hrv=True,
        sleep_score=82,
        resting_hr=True,
        avg_rr=14.5,
    )


def app_client(settings: GarminDbSettings) -> Iterator[TestClient]:
    yield from client_for(make_provider(settings))


@pytest.fixture
def client(corpus_settings: GarminDbSettings, corpus: Fixture) -> Iterator[TestClient]:
    yield from app_client(corpus_settings)


def break_the_schema(settings: GarminDbSettings) -> None:
    with sqlite3.connect(settings.db_dir / "garmin.db") as db:
        db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")


class TestRealWindows:
    def test_the_window_selects_the_rows_it_names(
        self, client: TestClient, corpus: Fixture
    ) -> None:
        """An hour of monitoring_hr is 30 rows at two-minute intervals. If an aware
        bound ever reached SQLite this count would silently be a wrong subset."""
        start = corpus.newest.start_utc
        response = client.get(
            "/api/v1/time-series",
            params={
                "metric": "heart_rate",
                "start": start.isoformat(),
                "end": (start + dt.timedelta(hours=1)).isoformat(),
            },
        )
        assert len(response.json()["samples"]) == 30

    def test_an_over_large_window_is_413(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard bounds the FETCH, not the response: without it a decade-wide
        request is a swap storm rather than an answer."""
        monkeypatch.setattr("garmin_health.limits.MAX_ROWS_SCANNED", 5)
        response = client.get("/api/v1/time-series", params={"metric": "heart_rate"})
        assert response.status_code == 413


class TestSleepSessions:
    def test_sessions_round_trip_with_their_stage_intervals_intact(
        self, client: TestClient, corpus: Fixture
    ) -> None:
        """The end-to-end proof of the one real serialization trap: stages reach a
        consumer only through SleepSession.stages, where the parametrized-generic
        path keeps end_timestamp."""
        payload = client.get("/api/v1/sleep-sessions").json()
        sessions = consumer_converter.structure(payload["data"], list[SleepSession])

        assert [s.id for s in sessions] == [n.session_id for n in reversed(corpus.nights)]
        newest = sessions[0]
        assert newest.start == corpus.newest.start_utc
        assert newest.end == corpus.newest.end_utc
        assert newest.source == "garmin"
        assert newest.stages is not None
        assert len(newest.stages.samples) == 8
        assert newest.stages.samples[0].end_timestamp == newest.stages.samples[1].timestamp
        assert newest.sleep_score is not None
        assert newest.sleep_score.value == 82.0
        assert newest.efficiency is not None
        assert newest.efficiency.value == pytest.approx(87.5)

    def test_the_window_selects_one_night(self, client: TestClient, corpus: Fixture) -> None:
        payload = client.get(
            "/api/v1/sleep-sessions", params={"start": corpus.newest.start_utc.isoformat()}
        ).json()
        assert len(payload["data"]) == 1


class TestDegraded:
    @pytest.fixture
    def broken_client(
        self, corpus_settings: GarminDbSettings, corpus: Fixture
    ) -> Iterator[TestClient]:
        break_the_schema(corpus_settings)
        yield from app_client(corpus_settings)

    def test_health_stays_ok_so_the_router_does_not_restart_loop_us(
        self, broken_client: TestClient
    ) -> None:
        """The container is working; its corpus needs a rebuild. Failing the probe
        would kill the very process the owner has to visit to fix it."""
        assert broken_client.get("/health").status_code == 200

    def test_the_owner_surface_stays_reachable(self, broken_client: TestClient) -> None:
        assert broken_client.get("/setup", headers=OWNER).status_code == 200

    def test_the_spec_surface_reports_503(self, broken_client: TestClient) -> None:
        assert (
            broken_client.get("/api/v1/time-series", params={"metric": "heart_rate"}).status_code
            == 503
        )
        assert broken_client.get("/api/v1/sleep-sessions").status_code == 503

    def test_the_fault_is_visible_to_the_owner(self, broken_client: TestClient) -> None:
        status = broken_client.get("/sync/status", headers=OWNER).json()
        assert status["serving"]["available"] is False
        assert "rebuild" in status["serving"]["fault"].lower()


class TestNeverSynced:
    @pytest.fixture
    def fresh_client(self, tmp_path: Path) -> Iterator[TestClient]:
        yield from app_client(GarminDbSettings(app_data_dir=tmp_path / "appdata"))

    def test_a_container_with_no_corpus_at_all_answers_emptily(
        self, fresh_client: TestClient
    ) -> None:
        """End to end, with no database files on disk: the degraded rule has to
        survive a GarminConnection that could not open anything."""
        assert fresh_client.get("/api/v1/metrics").json() == {"metrics": []}
        assert fresh_client.get("/api/v1/sleep-sessions").json() == {"data": []}
        series = fresh_client.get("/api/v1/time-series", params={"metric": "heart_rate"})
        assert series.status_code == 200
        assert series.json()["samples"] == []


class TestRebuildFromTheOwnerPage:
    """A schema mismatch is otherwise a dead end: nothing but the owner can delete
    the database files, and the container has no shell."""

    @pytest.fixture
    def broken(self, corpus_settings: GarminDbSettings, corpus: Fixture) -> Iterator[TestClient]:
        break_the_schema(corpus_settings)
        yield from app_client(corpus_settings)

    def test_the_setup_page_explains_the_fault(self, broken: TestClient) -> None:
        page = broken.get("/setup", headers=OWNER).text
        assert "rebuild" in page.lower()

    def test_the_setup_page_offers_a_rebuild_control(self, broken: TestClient) -> None:
        assert "action='/rebuild'" in broken.get("/setup", headers=OWNER).text

    def test_a_healthy_corpus_offers_no_rebuild_control(self, client: TestClient) -> None:
        """A destructive action should not be sitting there when nothing is wrong."""
        assert "action='/rebuild'" not in client.get("/setup", headers=OWNER).text

    def test_rebuild_is_owner_gated(self, broken: TestClient) -> None:
        assert broken.request("POST", "/rebuild").status_code == 401

    def test_rebuild_is_accepted_and_runs_in_the_background(self, broken: TestClient) -> None:
        response = broken.post("/rebuild", headers=OWNER)
        assert response.status_code == 202
        assert response.json()["started"] is True

    def test_rebuild_clears_the_fault_and_restores_service(self, broken: TestClient) -> None:
        """End to end: the files are deleted, the current schema is created from
        nothing, and the reader is reset so it stops pointing at the old inode."""
        assert broken.get("/api/v1/sleep-sessions").status_code == 503

        broken.post("/rebuild", headers=OWNER)
        for _ in range(100):
            if not broken.get("/sync/status", headers=OWNER).json()["running"]:
                break
            time.sleep(0.05)

        status = broken.get("/sync/status", headers=OWNER).json()
        assert status["last_sync"]["error"] is None
        assert status["serving"]["available"] is True
        assert broken.get("/api/v1/sleep-sessions").status_code == 200
