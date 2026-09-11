"""The /v1/* surface, exercised over real HTTP and read back with the spec's client."""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from health_data_service import MetricType
from health_data_service import SleepSession
from health_data_service import TimeSeries
from health_data_service.client import converter as consumer_converter
from litestar.testing import TestClient

from garmin_health.app import create_app
from garmin_health.auth import GarminAuthenticator
from garmin_health.config import Settings
from tests.fakes import RecordingFactory
from tests.fixtures import HOME_TZ_NAME
from tests.fixtures import Fixture
from tests.fixtures import build_fixture

OWNER = {"X-OpenHost-Is-Owner": "true"}


@pytest.fixture
def corpus_settings(tmp_path: Path) -> Settings:
    return Settings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)


@pytest.fixture
def corpus(corpus_settings: Settings) -> Fixture:
    return build_fixture(
        corpus_settings.health_data_dir,
        nights=3,
        heart_rate=True,
        hrv=True,
        sleep_score=82,
        resting_hr=True,
        avg_rr=14.5,
    )


@pytest.fixture
def client(corpus_settings: Settings, corpus: Fixture) -> Iterator[TestClient]:
    app = create_app(
        settings=corpus_settings,
        authenticator=GarminAuthenticator(
            corpus_settings, garmin_factory=RecordingFactory(needs_mfa=False)
        ),
    )
    with TestClient(app=app) as c:
        yield c


class TestMetrics:
    def test_the_catalog_uses_the_metrics_envelope(self, client: TestClient) -> None:
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200
        assert list(response.json()) == ["metrics"]

    def test_the_catalog_structures_with_the_consumers_converter(self, client: TestClient) -> None:
        payload = client.get("/api/v1/metrics").json()
        descriptors = consumer_converter.structure(payload["metrics"], list[MetricType])
        assert {d.metric_id for d in descriptors} == {
            "heart_rate",
            "hrv_rmssd",
            "sleep_score",
            "readiness_resting_heart_rate",
        }

    def test_it_is_not_owner_gated(self, client: TestClient) -> None:
        """/api/v1/* arrives through the router's internal service proxy, which stamps
        consumer headers rather than the owner one."""
        assert client.get("/api/v1/metrics").status_code == 200


class TestTimeSeries:
    def test_the_body_is_a_bare_time_series_with_no_envelope(self, client: TestClient) -> None:
        """client.get_time_series structures resp.json() itself. A {"data": ...}
        wrapper here makes every field come back missing."""
        payload = client.get("/api/v1/time-series", params={"metric": "heart_rate"}).json()
        assert payload["metric_id"] == "heart_rate"
        assert "data" not in payload

    def test_it_round_trips_through_the_consumers_converter(
        self, client: TestClient, corpus: Fixture
    ) -> None:
        response = client.get("/api/v1/time-series", params={"metric": "heart_rate", "limit": 10})
        series = consumer_converter.structure(response.json(), TimeSeries)
        assert series.unit == "bpm"
        assert series.source == "garmin"
        assert len(series.samples) == 10
        assert series.samples[0].timestamp == corpus.newest.start_utc

    def test_the_window_is_honoured(self, client: TestClient, corpus: Fixture) -> None:
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

    def test_it_accepts_the_timestamp_format_the_client_actually_sends(
        self, client: TestClient, corpus: Fixture
    ) -> None:
        """_to_params does str(v) on a datetime, which yields a SPACE separator
        rather than a T. Rejecting that would make every real consumer see a 400
        and read it as 'this provider has nothing'."""
        start = corpus.newest.start_utc
        response = client.get(
            "/api/v1/time-series",
            params={
                "metric": "heart_rate",
                "start": str(start),
                "end": str(start + dt.timedelta(hours=1)),
            },
        )
        assert response.status_code == 200
        assert len(response.json()["samples"]) == 30

    def test_a_naive_bound_is_read_as_utc(self, client: TestClient, corpus: Fixture) -> None:
        """The spec says timestamps are aware UTC, so a naive one is a consumer
        that forgot to say so, not a consumer meaning our container's local zone."""
        start = corpus.newest.start_utc
        response = client.get(
            "/api/v1/time-series",
            params={
                "metric": "heart_rate",
                "start": start.replace(tzinfo=None).isoformat(),
                "end": (start + dt.timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            },
        )
        assert response.status_code == 200
        assert len(response.json()["samples"]) == 30

    def test_an_unknown_metric_is_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/time-series", params={"metric": "nope"}).status_code == 404

    def test_a_known_metric_with_no_data_in_range_is_200_and_empty(
        self, client: TestClient
    ) -> None:
        """200 with samples: [] and 404 mean different things. The client collapses
        both to 'nothing', but only one of them is true."""
        response = client.get(
            "/api/v1/time-series",
            params={"metric": "heart_rate", "start": "2030-01-01T00:00:00+00:00"},
        )
        assert response.status_code == 200
        assert response.json()["samples"] == []

    def test_a_missing_metric_parameter_is_400(self, client: TestClient) -> None:
        assert client.get("/api/v1/time-series").status_code == 400

    def test_an_unparseable_bound_is_400(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/time-series", params={"metric": "heart_rate", "start": "last tuesday"}
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("limit", ["0", "-5"])
    def test_a_non_positive_limit_is_400(self, client: TestClient, limit: str) -> None:
        response = client.get(
            "/api/v1/time-series", params={"metric": "heart_rate", "limit": limit}
        )
        assert response.status_code == 400

    def test_an_over_large_window_is_413(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("garmin_health.garmin.sampling.MAX_ROWS_SCANNED", 5)
        response = client.get("/api/v1/time-series", params={"metric": "heart_rate"})
        assert response.status_code == 413


class TestSleepSessions:
    def test_the_body_uses_the_data_envelope(self, client: TestClient) -> None:
        response = client.get("/api/v1/sleep-sessions")
        assert response.status_code == 200
        assert list(response.json()) == ["data"]

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

    def test_the_limit_is_honoured(self, client: TestClient) -> None:
        payload = client.get("/api/v1/sleep-sessions", params={"limit": 1}).json()
        assert len(payload["data"]) == 1

    def test_the_window_is_honoured(self, client: TestClient, corpus: Fixture) -> None:
        payload = client.get(
            "/api/v1/sleep-sessions", params={"start": corpus.newest.start_utc.isoformat()}
        ).json()
        assert len(payload["data"]) == 1

    def test_an_unparseable_bound_is_400(self, client: TestClient) -> None:
        assert client.get("/api/v1/sleep-sessions", params={"start": "soon"}).status_code == 400


class TestWorkouts:
    def test_the_list_is_an_empty_data_envelope(self, client: TestClient) -> None:
        """Out of scope this iteration, but 200-with-nothing is a cheaper answer
        for a consumer than a non-200 it has to interpret."""
        response = client.get("/api/v1/workouts")
        assert response.status_code == 200
        assert response.json() == {"data": []}

    def test_a_single_workout_is_404(self, client: TestClient) -> None:
        """client.get_workout returns None on a 404, which is exactly right."""
        assert client.get("/api/v1/workouts/anything").status_code == 404


class TestDegraded:
    @pytest.fixture
    def broken_client(self, corpus_settings: Settings, corpus: Fixture) -> Iterator[TestClient]:
        with sqlite3.connect(corpus_settings.db_dir / "garmin.db") as db:
            db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")
        app = create_app(
            settings=corpus_settings,
            authenticator=GarminAuthenticator(
                corpus_settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
        )
        with TestClient(app=app) as c:
            yield c

    def test_health_stays_ok_so_the_router_does_not_restart_loop_us(
        self, broken_client: TestClient
    ) -> None:
        """The container is working; its corpus needs a rebuild. Failing the probe
        would kill the very process the owner has to visit to fix it."""
        assert broken_client.get("/health").status_code == 200

    def test_the_owner_surface_stays_reachable(self, broken_client: TestClient) -> None:
        assert broken_client.get("/setup", headers=OWNER).status_code == 200

    def test_time_series_reports_503(self, broken_client: TestClient) -> None:
        assert (
            broken_client.get("/api/v1/time-series", params={"metric": "heart_rate"}).status_code
            == 503
        )

    def test_sleep_sessions_report_503(self, broken_client: TestClient) -> None:
        assert broken_client.get("/api/v1/sleep-sessions").status_code == 503

    def test_the_catalog_is_empty_rather_than_failing(self, broken_client: TestClient) -> None:
        assert broken_client.get("/api/v1/metrics").json() == {"metrics": []}

    def test_the_fault_is_visible_to_the_owner(self, broken_client: TestClient) -> None:
        status = broken_client.get("/sync/status", headers=OWNER).json()
        assert status["serving"]["available"] is False
        assert "rebuild" in status["serving"]["fault"].lower()


class TestNeverSynced:
    @pytest.fixture
    def fresh_client(self, tmp_path: Path) -> Iterator[TestClient]:
        settings = Settings(app_data_dir=tmp_path / "appdata")
        app = create_app(
            settings=settings,
            authenticator=GarminAuthenticator(
                settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
        )
        with TestClient(app=app) as c:
            yield c

    def test_every_endpoint_answers_emptily(self, fresh_client: TestClient) -> None:
        """A brand-new install has no data and no imported timezone. That is a
        normal state awaiting a sync, not a failure."""
        assert fresh_client.get("/api/v1/metrics").json() == {"metrics": []}
        assert fresh_client.get("/api/v1/sleep-sessions").json() == {"data": []}
        series = fresh_client.get("/api/v1/time-series", params={"metric": "heart_rate"})
        assert series.status_code == 200
        assert series.json()["samples"] == []


class TestRebuildFromTheOwnerPage:
    """A schema mismatch is otherwise a dead end: nothing but the owner can delete
    the database files, and the container has no shell."""

    @pytest.fixture
    def broken(self, corpus_settings: Settings, corpus: Fixture) -> Iterator[TestClient]:
        with sqlite3.connect(corpus_settings.db_dir / "garmin.db") as db:
            db.execute("UPDATE _attributes SET value = '1' WHERE key = 'db.version'")
        app = create_app(
            settings=corpus_settings,
            authenticator=GarminAuthenticator(
                corpus_settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
        )
        with TestClient(app=app) as c:
            yield c

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

    def test_rebuild_clears_the_fault_and_restores_service(
        self, broken: TestClient, corpus_settings: Settings
    ) -> None:
        """End to end: the files are deleted, the current schema is created from
        nothing, and the connection is reset so it stops pointing at the old inode."""
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


MANIFEST = Path(__file__).resolve().parents[1] / "openhost.toml"

# Every path the spec's own consumer client requests, read off client.py.
CLIENT_PATHS = (
    "/v1/metrics",
    "/v1/time-series",
    "/v1/sleep-sessions",
    "/v1/workouts",
    "/v1/workouts/abc123",
)


def declared_endpoint() -> str:
    manifest = tomllib.loads(MANIFEST.read_text())
    provides = manifest["services"]["v2"]["provides"]
    assert len(provides) == 1
    endpoint: str = provides[0]["endpoint"]
    return endpoint


def router_path(endpoint: str, client_path: str) -> str:
    """Where the router lands a consumer's request inside this app.

    Per the cross-app services doc: "Service requests land rooted at `endpoint`
    in the provider app, ie app-name.your-domain.com/<endpoint>/<route>". The
    router **prepends**; it does not replace.
    """
    parts = [p for p in (endpoint.strip("/"), client_path.strip("/")) if p]
    return "/" + "/".join(parts)


class TestManifestRoutingContract:
    """The manifest and the route table have to agree, or the app is invisible.

    This is the failure that produces no error anywhere: the router forwards to a
    path we do not serve, gets a 404, and the consumer's `_fan_out` reads any
    non-200 as "this provider has nothing". The dashboard then renders an empty
    page with nothing in any log to explain it.
    """

    def test_the_manifest_declares_exactly_the_spec_service(self) -> None:
        manifest = tomllib.loads(MANIFEST.read_text())
        provides = manifest["services"]["v2"]["provides"][0]
        # The pre-rename string the spec's client still hardcodes as SERVICE_URL,
        # and what health-dashboard's [[services.v2.consumes]] asks for.
        assert provides["service"] == "github.com/imbue-openhost/health-data-service-spec"

    @pytest.mark.parametrize("client_path", CLIENT_PATHS)
    def test_every_client_path_is_served_where_the_router_will_look(
        self, client: TestClient, client_path: str
    ) -> None:
        landed = router_path(declared_endpoint(), client_path)
        response = client.get(landed, params={"metric": "heart_rate"})
        # 404 here means the router would get a 404, which the consumer silently
        # reads as "no data". /v1/workouts/{id} answers 404 by design, so it is
        # checked for being routed at all rather than for a status.
        if client_path.startswith("/v1/workouts/"):
            assert "detail" in response.json()
        else:
            assert response.status_code == 200, f"router would 404 on {landed}"

    def test_the_endpoint_is_not_double_prefixed(self) -> None:
        """The bug this test exists for: endpoint="/v1/" plus a client request for
        /v1/metrics lands on /v1/v1/metrics."""
        landed = router_path(declared_endpoint(), "/v1/metrics")
        assert landed.count("/v1") == 1, f"endpoint doubles the v1 prefix: {landed}"

    def test_the_bare_v1_prefix_is_not_served(self, client: TestClient) -> None:
        """Only the path the router actually asks for is exposed. A second mount
        would be a public surface nothing uses and nothing tests against."""
        assert client.get("/v1/metrics").status_code == 404
