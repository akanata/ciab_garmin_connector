import datetime as dt
import inspect
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from garminconnect import Garmin
from garminconnect import GarminConnectAuthenticationError
from litestar.testing import TestClient

from garmin_health.app import create_app
from garmin_health.providers.garmindb.auth import GarminAuthenticator
from garmin_health.providers.garmindb.config_file import read_config
from garmin_health.providers.garmindb.preferences import DOWNLOADABLE_STATS
from garmin_health.providers.garmindb.preferences import STAT_LABELS
from garmin_health.providers.garmindb.preferences import SYNC_INTERVAL_CHOICES
from garmin_health.providers.garmindb.preferences import ImportPreferences
from garmin_health.providers.garmindb.preferences import load_preferences
from garmin_health.providers.garmindb.preferences import save_preferences
from garmin_health.providers.garmindb.settings import GarminDbSettings
from garmin_health.providers.garmindb.sync import TableStat
from garmin_health.routes.owner import MINT_SNIPPET
from garmin_health.routes.owner import _join_names
from tests.providers.garmindb.fakes import FakeIngest
from tests.providers.garmindb.fakes import RecordingFactory

OWNER = {"X-OpenHost-Is-Owner": "true"}


@pytest.fixture
def settings(tmp_path: Path) -> GarminDbSettings:
    """Overrides the generic fixture: every route exercised here is GarminDB's.

    This whole module moves under ``tests/providers/garmindb/`` in Phase 4, when
    the page splits into a generic shell and the provider's own fragments.
    """
    return GarminDbSettings(app_data_dir=tmp_path / "appdata")


@pytest.fixture
def factory() -> RecordingFactory:
    return RecordingFactory(needs_mfa=False)


@pytest.fixture
def client(settings: GarminDbSettings, factory: RecordingFactory) -> Iterator[TestClient]:
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as c:
        yield c


def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_stays_ok_while_unlinked(client: TestClient) -> None:
    """The router restarts a container whose health check fails. An unlinked
    account is a normal state awaiting owner input, not a broken process."""
    assert client.get("/health").status_code == 200
    assert client.get("/setup/status", headers=OWNER).json()["state"] == "not_linked"


def test_health_needs_no_owner_header(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/setup"),
        ("GET", "/setup/status"),
        ("POST", "/setup/credentials"),
        ("POST", "/setup/mfa"),
        ("POST", "/setup/unlink"),
    ],
)
def test_setup_surface_is_owner_gated(client: TestClient, method: str, path: str) -> None:
    assert client.request(method, path).status_code == 401


def test_a_forged_owner_header_value_is_not_accepted(client: TestClient) -> None:
    assert client.get("/setup", headers={"X-OpenHost-Is-Owner": "false"}).status_code == 401
    assert client.get("/setup", headers={"X-OpenHost-Is-Owner": "1"}).status_code == 401


def test_setup_page_renders_for_the_owner(client: TestClient) -> None:
    response = client.get("/setup", headers=OWNER)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Garmin" in response.text


def test_setup_page_warns_that_the_password_is_replayed(client: TestClient) -> None:
    """There is no OAuth consent dialog for this API. The owner is handing over a
    real Garmin password and the page has to say so."""
    assert "password" in client.get("/setup", headers=OWNER).text.lower()


def test_credentials_post_links_the_account(client: TestClient) -> None:
    response = client.post(
        "/setup/credentials",
        data={"email": "rider@example.com", "password": "hunter2"},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/setup/status", headers=OWNER).json()["state"] == "linked"


def test_mfa_flow_moves_through_awaiting_to_linked(
    settings: GarminDbSettings, factory: RecordingFactory
) -> None:
    factory.client_kwargs["needs_mfa"] = True
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        client.post(
            "/setup/credentials",
            data={"email": "rider@example.com", "password": "hunter2"},
            headers=OWNER,
        )
        assert client.get("/setup/status", headers=OWNER).json()["state"] == "awaiting_mfa"
        assert "code" in client.get("/setup", headers=OWNER).text.lower()

        client.post("/setup/mfa", data={"code": "123456"}, headers=OWNER)
        assert client.get("/setup/status", headers=OWNER).json()["state"] == "linked"


def test_bad_credentials_render_an_error_rather_than_a_500(
    settings: GarminDbSettings,
) -> None:
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("bad password"))
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        response = client.post(
            "/setup/credentials",
            data={"email": "rider@example.com", "password": "wrong"},
            headers=OWNER,
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert "could not" in response.text.lower() or "failed" in response.text.lower()
        assert client.get("/setup/status", headers=OWNER).json()["state"] == "not_linked"


def test_missing_form_fields_are_rejected_cleanly(client: TestClient) -> None:
    response = client.post(
        "/setup/credentials", data={"email": ""}, headers=OWNER, follow_redirects=True
    )
    assert response.status_code in (200, 400)
    assert client.get("/setup/status", headers=OWNER).json()["state"] == "not_linked"


def test_unlink_returns_to_not_linked(client: TestClient) -> None:
    client.post(
        "/setup/credentials",
        data={"email": "rider@example.com", "password": "hunter2"},
        headers=OWNER,
    )
    client.post("/setup/unlink", headers=OWNER)
    assert client.get("/setup/status", headers=OWNER).json()["state"] == "not_linked"


def test_the_password_is_never_echoed_back_into_the_page(client: TestClient) -> None:
    client.post(
        "/setup/credentials",
        data={"email": "rider@example.com", "password": "hunter2"},
        headers=OWNER,
    )
    assert "hunter2" not in client.get("/setup", headers=OWNER).text


def test_setup_page_escapes_the_email(settings: GarminDbSettings) -> None:
    """The email is owner-supplied and lands in HTML; it must not be able to close
    an attribute and inject markup."""
    factory = RecordingFactory(needs_mfa=False)
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        client.post(
            "/setup/credentials",
            data={"email": '"><script>alert(1)</script>', "password": "hunter2"},
            headers=OWNER,
        )
        assert "<script>alert(1)</script>" not in client.get("/setup", headers=OWNER).text


def test_a_failed_sign_in_message_does_not_survive_a_refresh(settings: GarminDbSettings) -> None:
    """Regression: the failure was stored on the authenticator and re-rendered on
    every GET, so /setup kept reporting a sign-in failure indefinitely."""
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("bad password"))
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        first = client.post(
            "/setup/credentials",
            data={"email": "rider@example.com", "password": "wrong"},
            headers=OWNER,
            follow_redirects=True,
        )
        assert "failed" in first.text.lower()

        refreshed = client.get("/setup", headers=OWNER)
        assert "failed" not in refreshed.text.lower()
        assert "class='error'" not in refreshed.text


def test_a_failed_mfa_message_does_not_survive_a_refresh(settings: GarminDbSettings) -> None:
    factory = RecordingFactory(needs_mfa=True, mfa_code="654321")
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        client.post(
            "/setup/credentials",
            data={"email": "rider@example.com", "password": "hunter2"},
            headers=OWNER,
        )
        first = client.post(
            "/setup/mfa", data={"code": "000000"}, headers=OWNER, follow_redirects=True
        )
        assert "not accepted" in first.text.lower()
        assert "not accepted" not in client.get("/setup", headers=OWNER).text.lower()


def test_status_endpoint_reports_the_error_without_consuming_it(settings: GarminDbSettings) -> None:
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("bad password"))
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        client.post(
            "/setup/credentials",
            data={"email": "rider@example.com", "password": "wrong"},
            headers=OWNER,
            follow_redirects=False,
        )
        assert client.get("/setup/status", headers=OWNER).json()["error"] is not None
        # Polling status must not steal the message the page still has to show.
        assert "failed" in client.get("/setup", headers=OWNER).text.lower()
        assert client.get("/setup/status", headers=OWNER).json()["error"] is None


def test_the_awaiting_mfa_prompt_is_not_a_flash(settings: GarminDbSettings) -> None:
    """State-derived guidance must persist across refreshes, unlike an error."""
    factory = RecordingFactory(needs_mfa=True)
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        client.post(
            "/setup/credentials",
            data={"email": "rider@example.com", "password": "hunter2"},
            headers=OWNER,
        )
        assert "code" in client.get("/setup", headers=OWNER).text.lower()
        assert "code" in client.get("/setup", headers=OWNER).text.lower()


def test_sign_in_form_declares_an_in_flight_message(client: TestClient) -> None:
    """A real Garmin sign-in is a multi-second SSO round-trip; the page has to say
    something is happening or the owner assumes the button did nothing."""
    page = client.get("/setup", headers=OWNER).text
    assert "data-busy=" in page
    assert "Contacting Garmin" in page
    assert "spinner" in page


def test_mfa_form_declares_an_in_flight_message(settings: GarminDbSettings) -> None:
    factory = RecordingFactory(needs_mfa=True)
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(settings, garmin_factory=factory),
    )
    with TestClient(app=app) as client:
        client.post(
            "/setup/credentials",
            data={"email": "rider@example.com", "password": "hunter2"},
            headers=OWNER,
        )
        page = client.get("/setup", headers=OWNER).text
        assert "data-busy=" in page
        assert "Verifying" in page


def test_the_form_still_posts_without_javascript(client: TestClient) -> None:
    """The busy widget is progressive enhancement: the plain form POST is what
    actually submits, so a blocked inline script must not break linking."""
    response = client.post(
        "/setup/credentials",
        data={"email": "rider@example.com", "password": "hunter2"},
        headers=OWNER,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/setup/status", headers=OWNER).json()["state"] == "linked"


def wait_for_sync(client: TestClient, timeout: float = 5.0) -> dict:
    """Poll /sync/status until the backgrounded sync lands, as a browser would.

    POST /sync is deliberately fire-and-forget: a first backfill runs for tens of
    minutes, so the response cannot wait for the result.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get("/sync/status", headers=OWNER).json()
        if body["last_sync"] is not None and not body["running"]:
            return body
        time.sleep(0.02)
    raise AssertionError("the sync did not finish within the timeout")


def linked_client(
    settings: GarminDbSettings, ingest: FakeIngest | None = None
) -> tuple[TestClient, FakeIngest]:
    """A TestClient whose account is already linked, with a fake ingest behind it."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text('{"di_refresh_token": "r"}')
    ingest = ingest or FakeIngest()
    app = create_app(
        provider_settings=settings,
        authenticator=GarminAuthenticator(
            settings, garmin_factory=RecordingFactory(needs_mfa=False)
        ),
        ingest_factory=lambda: ingest,
    )
    return TestClient(app=app), ingest


class TestSyncEndpoints:
    @pytest.mark.parametrize(("method", "path"), [("POST", "/sync"), ("GET", "/sync/status")])
    def test_sync_surface_is_owner_gated(
        self, settings: GarminDbSettings, method: str, path: str
    ) -> None:
        client, _ = linked_client(settings)
        with client:
            assert client.request(method, path).status_code == 401

    def test_status_reports_link_state_and_interval(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            body = client.get("/sync/status", headers=OWNER).json()
        assert body["link_state"] == "linked"
        assert body["running"] is False
        assert body["last_sync"] is None
        assert body["interval_seconds"] > 0

    def test_trigger_accepts_and_reports_that_it_started(self, settings: GarminDbSettings) -> None:
        client, ingest = linked_client(settings)
        with client:
            response = client.post("/sync", headers=OWNER)
            assert response.status_code == 202
            assert response.json()["started"] is True
            assert wait_for_sync(client)["last_sync"] is not None
        assert ingest.calls.count("download") == 1

    def test_trigger_is_refused_while_the_account_is_unlinked(
        self, settings: GarminDbSettings
    ) -> None:
        """Syncing needs the saved token; silently doing nothing would look like a
        working sync that never produces data."""
        app = create_app(
            provider_settings=settings,
            authenticator=GarminAuthenticator(
                settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
            ingest_factory=lambda: FakeIngest(),
        )
        with TestClient(app=app) as client:
            response = client.post("/sync", headers=OWNER)
            assert response.status_code == 409
            assert "link" in response.json()["detail"].lower()

    def test_status_surfaces_a_failed_sync(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings, FakeIngest(fail_on="download"))
        with client:
            client.post("/sync", headers=OWNER)
            last = wait_for_sync(client)["last_sync"]
        assert last["error"] is not None
        assert last["phase"] == "download"

    def test_status_surfaces_a_sync_that_changed_nothing(self, settings: GarminDbSettings) -> None:
        """The signature of GarminDB's importers swallowing every per-file error."""
        same = {"sleep": TableStat(rows=2, latest="2026-06-14T23:00:00")}
        client, _ = linked_client(settings, FakeIngest(stats_sequence=[same, same]))
        with client:
            client.post("/sync", headers=OWNER)
            last = wait_for_sync(client)["last_sync"]
        assert last["error"] is None
        assert last["changed"] is False
        assert last["tables"]["sleep"]["rows"] == 2

    def test_health_stays_ok_while_a_sync_is_failing(self, settings: GarminDbSettings) -> None:
        """Failing the probe would make the router restart a container whose only
        problem is that Garmin is unreachable."""
        client, _ = linked_client(settings, FakeIngest(fail_on="download"))
        with client:
            client.post("/sync", headers=OWNER)
            assert client.get("/health").status_code == 200


class TestSetupShowsSyncState:
    def test_setup_offers_a_sync_button_once_linked(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "/sync" in page
        assert "sync" in page.lower()

    def test_setup_reports_the_last_sync(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            client.post("/sync", headers=OWNER)
            wait_for_sync(client)
            page = client.get("/setup", headers=OWNER).text
        assert "Last sync" in page

    def test_setup_says_when_nothing_has_synced_yet(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "not synced yet" in page.lower()

    def test_an_unlinked_setup_page_offers_no_sync_button(self, settings: GarminDbSettings) -> None:
        app = create_app(
            provider_settings=settings,
            authenticator=GarminAuthenticator(
                settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
            ingest_factory=lambda: FakeIngest(),
        )
        with TestClient(app=app) as client:
            assert "action='/sync'" not in client.get("/setup", headers=OWNER).text


class TestImportScopeControls:
    """The owner sets how far back to go and which metrics to fetch, from the page."""

    def test_the_form_is_rendered_with_the_current_scope(self, settings: GarminDbSettings) -> None:
        save_preferences(
            settings,
            ImportPreferences(
                start_date=dt.date(2024, 3, 1), enabled_stats=frozenset({"sleep", "hrv"})
            ),
        )
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text

        assert "value='2024-03-01'" in page
        assert "name='stats' value='sleep' checked" in page
        assert "name='stats' value='hrv' checked" in page
        assert "name='stats' value='monitoring' checked" not in page

    def test_every_downloadable_metric_gets_a_checkbox(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        for stat in DOWNLOADABLE_STATS:
            assert f"value='{stat}'" in page

    def test_saving_the_scope_persists_it(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            response = client.post(
                "/setup/import",
                data={"start_date": "2025-02-01", "stats": ["sleep", "rhr"]},
                headers=OWNER,
                follow_redirects=False,
            )
        assert response.status_code == 303
        saved = load_preferences(settings)
        assert saved.start_date == dt.date(2025, 2, 1)
        assert saved.enabled_stats == frozenset({"sleep", "rhr"})

    def test_the_scope_reaches_the_garmindb_config(self, settings: GarminDbSettings) -> None:
        """Saving has to change what the next sync actually downloads, not just
        what the page displays."""
        client, _ = linked_client(settings)
        with client:
            client.post(
                "/setup/import",
                data={"start_date": "2025-02-01", "stats": ["sleep"]},
                headers=OWNER,
            )
        raw = read_config(settings)
        assert raw["data"]["sleep_start_date"] == "2025-02-01"
        assert raw["enabled_stats"]["sleep"] is True
        assert raw["enabled_stats"]["monitoring"] is False

    def test_deselecting_everything_pauses_imports(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            client.post("/setup/import", data={"start_date": "2025-02-01"}, headers=OWNER)
        assert load_preferences(settings).enabled_stats == frozenset()

    def test_the_page_says_so_when_nothing_is_selected(self, settings: GarminDbSettings) -> None:
        save_preferences(
            settings,
            ImportPreferences(start_date=dt.date(2024, 3, 1), enabled_stats=frozenset()),
        )
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "no metrics" in page.lower()

    def test_a_bad_date_is_explained_and_nothing_is_saved(self, settings: GarminDbSettings) -> None:
        before = load_preferences(settings)
        client, _ = linked_client(settings)
        with client:
            response = client.post(
                "/setup/import",
                data={"start_date": "whenever", "stats": ["sleep"]},
                headers=OWNER,
            )
        assert response.status_code == 400
        assert "YYYY-MM-DD" in response.text
        assert load_preferences(settings) == before

    def test_a_future_date_is_refused(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            response = client.post(
                "/setup/import",
                data={"start_date": "2099-01-01", "stats": ["sleep"]},
                headers=OWNER,
            )
        assert response.status_code == 400
        assert "future" in response.text.lower()

    def test_the_scope_form_is_owner_gated(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            assert (
                client.post("/setup/import", data={"start_date": "2025-01-01"}).status_code == 401
            )


class TestCoverageDisplay:
    def test_each_metric_reports_what_it_holds(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "2024-03-01" in page
        assert "2026-09-10" in page
        for label in STAT_LABELS.values():
            assert label in page

    def test_a_metric_short_of_the_floor_offers_a_backfill(
        self, settings: GarminDbSettings
    ) -> None:
        """The whole reason the date control is not a no-op: incremental downloads
        only ever move forward from the newest row."""
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "action='/backfill'" in page
        assert "1,521" in page

    def test_a_complete_metric_offers_no_backfill(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings, FakeIngest(coverage_gap=False))
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "action='/backfill'" not in page

    def test_coverage_is_exposed_as_json(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            body = client.get("/sync/status", headers=OWNER).json()
        sleep = next(c for c in body["coverage"] if c["stat"] == "sleep")
        assert sleep["rows"] == 42
        assert sleep["missing_days"] == 1521


class TestBackfillTrigger:
    def test_it_starts_a_backfill(self, settings: GarminDbSettings) -> None:
        client, ingest = linked_client(settings)
        with client:
            response = client.post("/backfill", headers=OWNER)
            assert response.status_code == 202
            assert response.json()["started"] is True
            wait_for_sync(client)
        assert "backfill" in ingest.calls

    def test_it_is_refused_while_unlinked(self, settings: GarminDbSettings) -> None:
        """It really does talk to Garmin, unlike a rebuild."""
        app = create_app(
            provider_settings=settings,
            authenticator=GarminAuthenticator(
                settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
            ingest_factory=lambda: FakeIngest(),
        )
        with TestClient(app=app) as unlinked:
            assert unlinked.post("/backfill", headers=OWNER).status_code == 409

    def test_it_is_owner_gated(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            assert client.post("/backfill").status_code == 401


class TestProgressDisplay:
    def test_the_page_shows_the_step_a_running_sync_is_on(self, settings: GarminDbSettings) -> None:
        gate = threading.Event()
        client, _ = linked_client(settings, FakeIngest(block_on=gate))
        try:
            with client:
                client.post("/sync", headers=OWNER)
                for _ in range(100):
                    if client.get("/sync/status", headers=OWNER).json()["progress"]:
                        break
                    time.sleep(0.02)
                status = client.get("/sync/status", headers=OWNER).json()
                label = status["progress"]["label"]
                assert "Garmin" in label
                assert label in client.get("/setup", headers=OWNER).text
        finally:
            gate.set()

    def test_the_status_endpoint_reports_no_progress_while_idle(
        self, settings: GarminDbSettings
    ) -> None:
        client, _ = linked_client(settings)
        with client:
            assert client.get("/sync/status", headers=OWNER).json()["progress"] is None


class TestCoverageWording:
    """The table is the only place the owner learns what is actually there."""

    def test_a_metric_holding_nothing_is_not_called_complete(
        self, settings: GarminDbSettings
    ) -> None:
        """has_gap is False for an empty metric because a normal sync already
        starts it at the floor -- but "complete" would be a plain lie."""
        client, _ = linked_client(settings, FakeIngest(empty_metrics=True))
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "complete" not in page
        assert "starts at your date" in page
        assert "nothing yet" in page

    def test_a_paused_metric_that_holds_data_says_paused(self, settings: GarminDbSettings) -> None:
        """Switching a metric off does not delete what it already downloaded, and
        the row still has to say what is there."""
        client, _ = linked_client(settings, FakeIngest(disabled_metrics=True))
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "paused" in page
        assert "42 rows" in page
        assert "action='/backfill'" not in page

    def test_the_gap_summary_names_the_metrics_as_a_sentence(
        self, settings: GarminDbSettings
    ) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "Heart rate, Sleep, Resting heart rate and Heart rate variability" in page

    @pytest.mark.parametrize(
        ("names", "expected"),
        [
            ([], ""),
            (["Sleep"], "Sleep"),
            (["Sleep", "HRV"], "Sleep and HRV"),
            (["Sleep", "HRV", "Heart rate"], "Sleep, HRV and Heart rate"),
        ],
    )
    def test_names_are_joined_as_prose(self, names: list[str], expected: str) -> None:
        assert _join_names(names) == expected


TOKEN_JSON = json.dumps(
    {"di_token": "access", "di_refresh_token": "refresh", "di_client_id": "client"}
)


class TestTokenImportPage:
    """The way past a Cloudflare bot challenge on Garmin's sign-in portal."""

    def test_the_unlinked_page_offers_it(self, client: TestClient) -> None:
        page = client.get("/setup", headers=OWNER).text
        assert "action='/setup/token'" in page
        assert "bot challenge" in page.lower()

    def test_it_explains_how_to_mint_one(self, client: TestClient) -> None:
        """Nobody has garmin_tokens.json lying around; the page has to say how to
        produce it on a machine that can sign in."""
        page = client.get("/setup", headers=OWNER).text
        assert "garmin_tokens.json" in page
        assert "garminconnect" in page

    def test_a_linked_account_is_not_offered_it(self, settings: GarminDbSettings) -> None:
        """A destructive-looking alternative should not sit on a working page."""
        linked, _ = linked_client(settings)
        with linked:
            assert "action='/setup/token'" not in linked.get("/setup", headers=OWNER).text

    def test_importing_a_token_links_the_account(self, client: TestClient) -> None:
        response = client.post(
            "/setup/token",
            data={"token": TOKEN_JSON, "email": "rider@example.com"},
            headers=OWNER,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/setup/status", headers=OWNER).json()["state"] == "linked"

    def test_the_imported_token_is_what_syncs_will_read(
        self, client: TestClient, settings: GarminDbSettings
    ) -> None:
        client.post("/setup/token", data={"token": TOKEN_JSON}, headers=OWNER)
        manager_path = settings.token_file
        assert json.loads(manager_path.read_text())["di_refresh_token"] == "refresh"

    def test_a_bad_token_is_explained_on_the_page(self, client: TestClient) -> None:
        response = client.post(
            "/setup/token", data={"token": "{oops"}, headers=OWNER, follow_redirects=True
        )
        assert "valid JSON" in response.text
        assert client.get("/setup/status", headers=OWNER).json()["state"] == "not_linked"

    def test_a_truncated_token_names_the_missing_fields(self, client: TestClient) -> None:
        response = client.post(
            "/setup/token",
            data={"token": json.dumps({"di_token": "t"})},
            headers=OWNER,
            follow_redirects=True,
        )
        assert "di_refresh_token" in response.text

    def test_the_error_does_not_persist_across_refreshes(self, client: TestClient) -> None:
        """One mistyped paste must not accuse the owner for ever."""
        client.post("/setup/token", data={"token": "{oops"}, headers=OWNER)
        assert "valid JSON" not in client.get("/setup", headers=OWNER).text

    def test_the_token_is_never_echoed_back_into_the_page(self, client: TestClient) -> None:
        """It is a bearer credential; it must not end up in browser history, a
        screenshot, or a re-rendered form field."""
        response = client.post(
            "/setup/token",
            data={"token": TOKEN_JSON.replace("refresh", "SECRETVALUE")},
            headers=OWNER,
            follow_redirects=True,
        )
        assert "SECRETVALUE" not in response.text

    def test_it_is_owner_gated(self, client: TestClient) -> None:
        assert client.post("/setup/token", data={"token": TOKEN_JSON}).status_code == 401


def test_the_mint_snippet_matches_the_installed_library() -> None:
    """The page tells the owner to run this on another machine, so a library
    rename must break the build rather than ship instructions that fail.

    ``g.garth.dump`` is the *older* library's spelling and does not exist on
    garminconnect 0.3.11 -- this pins the one that does.
    """
    client = Garmin()
    assert "g.client.dump(" in MINT_SNIPPET
    assert "g.garth" not in MINT_SNIPPET
    assert hasattr(client, "client")
    assert callable(client.client.dump)
    # The constructor call in the snippet is positional (email, password).
    assert 'Garmin("you@example.com", "your-password")' in MINT_SNIPPET
    parameters = list(inspect.signature(Garmin.__init__).parameters)
    assert parameters[1:3] == ["email", "password"]


class TestSyncIntervalControl:
    """How often the background sync runs, chosen on /setup."""

    def test_the_form_offers_the_intervals_with_the_saved_one_selected(
        self, settings: GarminDbSettings
    ) -> None:
        save_preferences(
            settings,
            ImportPreferences(
                start_date=dt.date(2024, 3, 1),
                enabled_stats=frozenset({"sleep"}),
                sync_interval_seconds=1800,
            ),
        )
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "name='sync_interval'" in page
        assert "value='1800' selected" in page
        for seconds in SYNC_INTERVAL_CHOICES:
            assert f"value='{seconds}'" in page

    def test_saving_a_new_interval_takes_effect(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            client.post(
                "/setup/import",
                data={"start_date": "2025-02-01", "stats": ["sleep"], "sync_interval": "900"},
                headers=OWNER,
            )
            status = client.get("/sync/status", headers=OWNER).json()
        assert load_preferences(settings).sync_interval_seconds == 900
        assert status["interval_seconds"] == 900

    def test_an_interval_not_offered_is_refused(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            response = client.post(
                "/setup/import",
                data={"start_date": "2025-02-01", "stats": ["sleep"], "sync_interval": "60"},
                headers=OWNER,
            )
        assert response.status_code == 400
        assert load_preferences(settings).sync_interval_seconds == settings.sync_interval_seconds

    def test_the_page_says_when_the_next_automatic_sync_is(
        self, settings: GarminDbSettings
    ) -> None:
        """Whether a sync needs pressing at all should not need reading the code."""
        client, _ = linked_client(settings)
        with client:
            client.post("/sync", headers=OWNER)
            wait_for_sync(client)
            page = client.get("/setup", headers=OWNER).text
        assert "Next automatic sync" in page

    def test_before_any_sync_the_page_says_one_is_on_its_way(
        self, settings: GarminDbSettings
    ) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "automatic sync" in page.lower()

    def test_status_reports_when_the_next_sync_is_due(self, settings: GarminDbSettings) -> None:
        client, _ = linked_client(settings)
        with client:
            client.post("/sync", headers=OWNER)
            body = wait_for_sync(client)
        assert body["next_sync_at"] is not None
