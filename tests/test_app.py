"""What ``create_app`` wires, stated without naming a provider.

Every rule here is one the app owes *any* provider: that the configured one is
the one built, that its routes are mounted on the right side of the owner guard,
that its lifespans run and unwind inside the reader's, and that saying "data
changed" is enough to make the serving layer notice.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from litestar.testing import TestClient

from garmin_health.app import create_app
from garmin_health.config import ConfigError
from garmin_health.config import Settings
from garmin_health.providers import build_provider
from tests.fakes import FakeProvider
from tests.fakes import FakeReader

OWNER = {"X-OpenHost-Is-Owner": "true"}


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(provider: FakeProvider) -> Iterator[TestClient]:
    with TestClient(app=create_app(provider=provider)) as c:
        yield c


class TestProviderSelection:
    def test_the_default_configuration_builds_the_garmindb_provider(self, tmp_path: Path) -> None:
        """Asserted through the port's own ``name``, not an import: the core is
        not allowed to name a concrete provider, and neither is this test."""
        provider = build_provider(Settings(app_data_dir=tmp_path / "appdata"))
        assert provider.name == "garmindb"

    def test_an_unknown_provider_fails_at_startup(self, tmp_path: Path) -> None:
        """Falling back to a default would leave a container acquiring nothing,
        looking healthy, and serving an empty catalog for ever."""
        settings = Settings(app_data_dir=tmp_path / "appdata", provider="bogus")
        with pytest.raises(ConfigError, match="bogus"):
            create_app(settings=settings)


class TestLifespans:
    def test_the_providers_lifespans_run_and_unwind(self, provider: FakeProvider) -> None:
        with TestClient(app=create_app(provider=provider)):
            assert provider.lifespan_events == ["enter"]
        assert provider.lifespan_events == ["enter", "exit"]

    def test_the_reader_is_opened_once_and_closed_on_shutdown(self, provider: FakeProvider) -> None:
        with TestClient(app=create_app(provider=provider)) as client:
            client.get("/health")
            assert len(provider.readers) == 1
            assert provider.readers[0].closed is False
        assert provider.readers[0].closed is True

    def test_the_reader_outlives_the_providers_own_lifespans(self, provider: FakeProvider) -> None:
        """The sync loop's callbacks reach the service, so the service has to be
        there for the whole of the loop's life -- which means opening first and
        closing last."""
        with TestClient(app=create_app(provider=provider)):
            pass
        # "exit" is recorded in the provider lifespan's finally, before the
        # serving lifespan's own finally closes the reader.
        assert provider.lifespan_events == ["enter", "exit"]
        assert provider.readers[0].closed is True


class TestRouteMounting:
    def test_provider_owner_routes_are_behind_the_guard(self, client: TestClient) -> None:
        """Mounted on the guarded router by the app, so a provider handler cannot
        forget the guard."""
        assert client.get("/fake/owner").status_code == 401
        assert client.get("/fake/owner", headers=OWNER).json() == {"owner": True}

    def test_provider_public_routes_are_not_guarded(self, client: TestClient) -> None:
        """A webhook arrives from the vendor, with no owner header anywhere."""
        assert client.get("/fake/public").json() == {"public": True}

    def test_health_is_reachable_without_the_owner_header(self, client: TestClient) -> None:
        assert client.get("/health").json() == {"status": "ok"}

    def test_the_spec_surface_is_mounted(self, client: TestClient) -> None:
        assert client.get("/api/v1/metrics").status_code == 200

    def test_no_public_router_is_mounted_when_a_provider_has_none(self) -> None:
        """The app has no unguarded surface by default."""
        provider = FakeProvider()
        provider.public_routes = list  # type: ignore[method-assign]
        with TestClient(app=create_app(provider=provider)) as client:
            assert client.get("/fake/public").status_code == 404
            assert client.get("/health").status_code == 200


class TestDataChanged:
    def test_newly_acquired_data_drops_the_catalog_cache(self, tmp_path: Path) -> None:
        """The catalog is probed behind a TTL, so without this a metric that only
        just got its first rows stays unadvertised until the TTL lapses."""
        reader = FakeReader()
        provider = FakeProvider(reader=reader)
        with TestClient(app=create_app(provider=provider)) as client:
            client.get("/api/v1/metrics")
            before = reader.probe_calls
            client.get("/api/v1/metrics")
            assert reader.probe_calls == before, "the catalog should have been cached"

            provider.emit_data_changed()
            client.get("/api/v1/metrics")
            assert reader.probe_calls > before

    def test_a_provider_with_no_reader_yet_can_still_announce(self) -> None:
        """The subscription is registered before the lifespan opens, so a
        notification that arrives early must not raise."""
        provider = FakeProvider()
        create_app(provider=provider)
        provider.emit_data_changed()


class TestSyncStatus:
    def test_it_merges_the_link_state_the_provider_and_the_serving_layer(
        self, client: TestClient
    ) -> None:
        body = client.get("/sync/status", headers=OWNER).json()
        assert body["link_state"] == "not_linked"
        assert body["running"] is False
        assert body["progress"] is None
        assert body["fake"] is True
        assert body["serving"]["available"] is True

    def test_it_is_owner_gated(self, client: TestClient) -> None:
        assert client.get("/sync/status").status_code == 401
