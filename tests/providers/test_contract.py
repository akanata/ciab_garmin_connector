"""One contract, run against every provider there is.

The point of a port is that the app can hold any implementation of it. These
tests are the executable statement of what "any" means, and they run against both
the test double and the real GarminDB provider -- so the double cannot drift into
being easier to satisfy than the thing it stands in for, which is the usual way a
seam turns out to be imaginary.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from health_data_service import IntervalSample
from health_data_service import Sample
from health_data_service import SleepSession
from litestar.testing import TestClient

from garmin_health.app import create_app
from garmin_health.ports import HealthReader
from garmin_health.ports import LinkState
from garmin_health.ports import LinkStatus
from garmin_health.ports import Provider
from garmin_health.ports import SetupView
from garmin_health.providers.garmindb.provider import GarminDbProvider
from garmin_health.providers.garmindb.settings import GarminDbSettings
from tests.fakes import FakeProvider
from tests.providers.garmindb.fakes import FakeIngest
from tests.providers.garmindb.fakes import RecordingFactory
from tests.providers.garmindb.fixtures import HOME_TZ_NAME
from tests.providers.garmindb.fixtures import build_fixture

OWNER = {"X-OpenHost-Is-Owner": "true"}


def _garmindb_provider(tmp_path: Path) -> GarminDbProvider:
    """A real provider over a real corpus, with only the network faked out."""
    from garmin_health.providers.garmindb.auth import GarminAuthenticator  # noqa: PLC0415

    settings = GarminDbSettings(app_data_dir=tmp_path / "appdata", home_tz=HOME_TZ_NAME)
    build_fixture(
        settings.health_data_dir,
        nights=3,
        heart_rate=True,
        hrv=True,
        sleep_score=82,
        resting_hr=True,
        avg_rr=14.5,
    )
    return GarminDbProvider(
        settings,
        authenticator=GarminAuthenticator(
            settings, garmin_factory=RecordingFactory(needs_mfa=False)
        ),
        ingest_factory=lambda: FakeIngest(),
    )


@pytest.fixture(params=["fake", "garmindb"])
def provider(request: pytest.FixtureRequest, tmp_path: Path) -> Provider:
    if request.param == "fake":
        return FakeProvider()
    return _garmindb_provider(tmp_path)


@pytest.fixture
def reader(provider: Provider) -> Iterator[HealthReader]:
    opened = provider.open_reader()
    try:
        yield opened
    finally:
        opened.close()


class TestProviderContract:
    def test_it_is_a_provider(self, provider: Provider) -> None:
        assert isinstance(provider, Provider)

    def test_it_names_itself(self, provider: Provider) -> None:
        assert provider.name
        assert provider.display_name


class TestReaderContract:
    def test_what_it_opens_is_a_reader(self, reader: HealthReader) -> None:
        assert isinstance(reader, HealthReader)

    def test_every_catalog_key_is_its_own_metric_id(self, reader: HealthReader) -> None:
        """The key is what a consumer asks for; a mismatch is a 404 for a metric
        the catalog just advertised."""
        for key, entry in reader.metrics.items():
            assert key == entry.descriptor.metric_id

    def test_no_metric_is_interval_valued(self, reader: HealthReader) -> None:
        """``TimeSeries.samples`` is bare ``list[Sample]`` and the consumer's
        structure hook resolves by MRO, so an IntervalSample would arrive with
        ``end_timestamp`` silently discarded."""
        for entry in reader.metrics.values():
            samples = entry.build(None, None, 5)
            assert not any(isinstance(s, IntervalSample) for s in samples)

    def test_every_builder_honours_its_limit(self, reader: HealthReader) -> None:
        for entry in reader.metrics.values():
            samples = entry.build(None, None, 5)
            assert len(samples) <= 5
            assert all(isinstance(s, Sample) for s in samples)

    def test_every_series_matches_its_descriptor(self, reader: HealthReader) -> None:
        for entry in reader.metrics.values():
            series = entry.series([])
            assert series.metric_id == entry.descriptor.metric_id
            assert series.unit == entry.descriptor.unit
            assert series.source == "garmin"

    def test_every_probe_answers_a_bool(self, reader: HealthReader) -> None:
        for entry in reader.metrics.values():
            assert isinstance(entry.probe(), bool)

    def test_sleep_sessions_honour_their_limit(self, reader: HealthReader) -> None:
        sessions = reader.sleep_sessions(None, None, 1)
        assert len(sessions) <= 1
        assert all(isinstance(s, SleepSession) for s in sessions)

    def test_it_reports_whether_it_holds_anything(self, reader: HealthReader) -> None:
        assert isinstance(reader.has_any_data(), bool)


class TestLinkContract:
    def test_status_is_a_link_status(self, provider: Provider) -> None:
        assert isinstance(provider.link.status(), LinkStatus)

    def test_a_fresh_provider_has_no_flash(self, provider: Provider) -> None:
        """An error is something that just happened, never a standing state."""
        assert provider.link.take_error() is None

    async def test_unlinking_leaves_it_not_linked(self, provider: Provider) -> None:
        assert (await provider.link.unlink()).state is LinkState.NOT_LINKED
        assert provider.link.status().state is LinkState.NOT_LINKED


class TestStatusContract:
    async def test_it_carries_the_keys_the_page_script_reads(self, provider: Provider) -> None:
        """BUSY_SCRIPT is generic and polls for exactly these two."""
        status = await provider.status()
        assert isinstance(status["running"], bool)
        assert "progress" in status


class TestSetupContract:
    @pytest.mark.parametrize("state", list(LinkState))
    async def test_it_renders_something_for_every_link_state(
        self, provider: Provider, state: LinkState
    ) -> None:
        view = SetupView(link=LinkStatus(state=state, account="rider@example.com"))
        assert await provider.render_setup(view)


class TestRouteContract:
    def test_every_owner_route_is_gated_when_mounted(self, provider: Provider) -> None:
        """Mounted by the app on a guarded router, so this holds for a provider
        that never thought about the guard at all."""
        routes = provider.owner_routes()
        with TestClient(app=create_app(provider=provider)) as client:
            for handler in routes:
                for path in handler.paths:
                    method = next(iter(handler.http_methods))
                    assert client.request(method, path).status_code == 401, path

    def test_public_routes_are_ungated(self, provider: Provider) -> None:
        """Whatever a provider declares public really is reachable without the
        owner header -- a webhook arrives from the vendor, not from a browser.

        That these paths are also declared in ``openhost.toml``'s
        ``public_paths`` is a manifest rule, not a provider one: it binds the
        provider this image actually ships, never a test double. It lands in
        ``TestManifestRoutingContract`` in Phase 5.
        """
        routes = provider.public_routes()
        with TestClient(app=create_app(provider=provider)) as client:
            for handler in routes:
                for path in handler.paths:
                    method = next(iter(handler.http_methods))
                    assert client.request(method, path).status_code != 401, path
