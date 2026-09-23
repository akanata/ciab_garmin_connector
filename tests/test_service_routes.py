"""``/api/v1/*``: status codes, envelopes, and the manifest that routes to them.

Mounted over a :class:`FakeReader`, with no app and no corpus, because none of
these rules are a provider's. The GarminDB half -- real sample counts, the scan
cap, the rebuild path -- is in ``tests/providers/garmindb/test_service_routes.py``.
"""

from __future__ import annotations

import datetime as dt
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from health_data_service import MetricType
from health_data_service import TimeSeries
from health_data_service.client import converter as consumer_converter
from litestar import Litestar
from litestar.datastructures import State
from litestar.testing import TestClient

from garmin_health.config import Settings
from garmin_health.providers import build_provider
from garmin_health.routes.service import v1_router
from garmin_health.service import HealthDataService
from tests.fakes import EPOCH
from tests.fakes import SAMPLE_COUNT
from tests.fakes import FakeReader


def client_for(reader: FakeReader) -> Iterator[TestClient]:
    app = Litestar(
        route_handlers=[v1_router],
        state=State({"health_service": HealthDataService(reader)}),
    )
    with TestClient(app=app) as c:
        yield c


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from client_for(FakeReader())


class TestMetrics:
    def test_the_catalog_uses_the_metrics_envelope(self, client: TestClient) -> None:
        response = client.get("/api/v1/metrics")
        assert response.status_code == 200
        assert list(response.json()) == ["metrics"]

    def test_the_catalog_structures_with_the_consumers_converter(self, client: TestClient) -> None:
        payload = client.get("/api/v1/metrics").json()
        descriptors = consumer_converter.structure(payload["metrics"], list[MetricType])
        assert {d.metric_id for d in descriptors} == {"heart_rate"}

    def test_it_is_not_owner_gated(self, client: TestClient) -> None:
        """/api/v1/* arrives through the router's internal service proxy, which
        stamps consumer headers rather than the owner one."""
        assert client.get("/api/v1/metrics").status_code == 200


class TestTimeSeries:
    def test_the_body_is_a_bare_time_series_with_no_envelope(self, client: TestClient) -> None:
        """client.get_time_series structures resp.json() itself. A {"data": ...}
        wrapper here makes every field come back missing."""
        payload = client.get("/api/v1/time-series", params={"metric": "heart_rate"}).json()
        assert payload["metric_id"] == "heart_rate"
        assert "data" not in payload

    def test_it_round_trips_through_the_consumers_converter(self, client: TestClient) -> None:
        response = client.get("/api/v1/time-series", params={"metric": "heart_rate"})
        series = consumer_converter.structure(response.json(), TimeSeries)
        assert series.unit == "bpm"
        assert series.source == "garmin"
        assert len(series.samples) == SAMPLE_COUNT
        assert series.samples[0].timestamp == EPOCH

    def test_the_window_is_honoured(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/time-series",
            params={
                "metric": "heart_rate",
                "start": EPOCH.isoformat(),
                "end": (EPOCH + dt.timedelta(minutes=6)).isoformat(),
            },
        )
        assert len(response.json()["samples"]) == 3

    def test_it_accepts_the_timestamp_format_the_client_actually_sends(
        self, client: TestClient
    ) -> None:
        """_to_params does str(v) on a datetime, which yields a SPACE separator
        rather than a T. Rejecting that would make every real consumer see a 400
        and read it as 'this provider has nothing'."""
        response = client.get(
            "/api/v1/time-series",
            params={
                "metric": "heart_rate",
                "start": str(EPOCH),
                "end": str(EPOCH + dt.timedelta(minutes=6)),
            },
        )
        assert response.status_code == 200
        assert len(response.json()["samples"]) == 3

    def test_a_naive_bound_is_read_as_utc(self, client: TestClient) -> None:
        """The spec says timestamps are aware UTC, so a naive one is a consumer
        that forgot to say so, not one meaning our container's local zone."""
        response = client.get(
            "/api/v1/time-series",
            params={
                "metric": "heart_rate",
                "start": EPOCH.replace(tzinfo=None).isoformat(),
                "end": (EPOCH + dt.timedelta(minutes=6)).replace(tzinfo=None).isoformat(),
            },
        )
        assert response.status_code == 200
        assert len(response.json()["samples"]) == 3

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


class TestSleepSessions:
    def test_the_body_uses_the_data_envelope(self, client: TestClient) -> None:
        response = client.get("/api/v1/sleep-sessions")
        assert response.status_code == 200
        assert list(response.json()) == ["data"]

    def test_the_limit_is_honoured(self, client: TestClient) -> None:
        payload = client.get("/api/v1/sleep-sessions", params={"limit": 1}).json()
        assert len(payload["data"]) == 1

    def test_the_window_is_honoured(self, client: TestClient) -> None:
        payload = client.get(
            "/api/v1/sleep-sessions",
            params={"start": (EPOCH + dt.timedelta(days=1)).isoformat()},
        ).json()
        assert payload["data"] == []

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
    def broken_client(self) -> Iterator[TestClient]:
        yield from client_for(FakeReader(fault="The schema on disk must be rebuilt."))

    def test_time_series_reports_503(self, broken_client: TestClient) -> None:
        assert (
            broken_client.get("/api/v1/time-series", params={"metric": "heart_rate"}).status_code
            == 503
        )

    def test_sleep_sessions_report_503(self, broken_client: TestClient) -> None:
        assert broken_client.get("/api/v1/sleep-sessions").status_code == 503

    def test_the_catalog_is_empty_rather_than_failing(self, broken_client: TestClient) -> None:
        assert broken_client.get("/api/v1/metrics").json() == {"metrics": []}


class TestNeverSynced:
    @pytest.fixture
    def fresh_client(self) -> Iterator[TestClient]:
        yield from client_for(FakeReader(ready=False, has_data=False))

    def test_every_endpoint_answers_emptily(self, fresh_client: TestClient) -> None:
        """A brand-new install has no data and no clock to place it on. That is a
        normal state awaiting a first acquisition, not a failure."""
        assert fresh_client.get("/api/v1/metrics").json() == {"metrics": []}
        assert fresh_client.get("/api/v1/sleep-sessions").json() == {"data": []}
        series = fresh_client.get("/api/v1/time-series", params={"metric": "heart_rate"})
        assert series.status_code == 200
        assert series.json()["samples"] == []


class TestNotReadyButPopulated:
    @pytest.fixture
    def stuck_client(self) -> Iterator[TestClient]:
        yield from client_for(FakeReader(ready=False, has_data=True))

    def test_it_refuses_rather_than_reporting_no_history(self, stuck_client: TestClient) -> None:
        """The other side of the same rule: this container holds data it cannot
        place on a real clock, and an empty 200 would deny the history exists."""
        response = stuck_client.get("/api/v1/time-series", params={"metric": "heart_rate"})
        assert response.status_code == 503
        assert stuck_client.get("/api/v1/sleep-sessions").status_code == 503


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

    def test_public_paths_matches_what_the_provider_serves_ungated(self, tmp_path: Path) -> None:
        """Both directions, because both failures are silent.

        A public route missing from ``public_paths`` is never forwarded to; a
        ``public_paths`` entry nothing serves is a declared door onto a 404.

        Vacuously true today -- every route is either the spec surface, reached
        through the router's internal service proxy, or owner-gated. It becomes
        load-bearing the moment a provider adds a webhook receiver, which is the
        whole reason an aggregator would need one.

        Asked of ``build_provider`` rather than a named provider, so this stays
        true of whichever one the image ships.
        """
        manifest = tomllib.loads(MANIFEST.read_text())
        declared = set(manifest["routing"]["public_paths"])
        provider = build_provider(Settings(app_data_dir=tmp_path / "appdata"))
        served = {path for handler in provider.public_routes() for path in handler.paths}
        assert served == declared
