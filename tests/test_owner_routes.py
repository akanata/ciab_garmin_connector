import time
from collections.abc import Iterator

import pytest
from garminconnect import GarminConnectAuthenticationError
from litestar.testing import TestClient

from garmin_health.app import create_app
from garmin_health.auth import GarminAuthenticator
from garmin_health.config import Settings
from garmin_health.sync import TableStat
from tests.fakes import FakeIngest
from tests.fakes import RecordingFactory

OWNER = {"X-OpenHost-Is-Owner": "true"}


@pytest.fixture
def factory() -> RecordingFactory:
    return RecordingFactory(needs_mfa=False)


@pytest.fixture
def client(settings: Settings, factory: RecordingFactory) -> Iterator[TestClient]:
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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
    settings: Settings, factory: RecordingFactory
) -> None:
    factory.client_kwargs["needs_mfa"] = True
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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
    settings: Settings,
) -> None:
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("bad password"))
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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


def test_setup_page_escapes_the_email(settings: Settings) -> None:
    """The email is owner-supplied and lands in HTML; it must not be able to close
    an attribute and inject markup."""
    factory = RecordingFactory(needs_mfa=False)
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
    )
    with TestClient(app=app) as client:
        client.post(
            "/setup/credentials",
            data={"email": '"><script>alert(1)</script>', "password": "hunter2"},
            headers=OWNER,
        )
        assert "<script>alert(1)</script>" not in client.get("/setup", headers=OWNER).text


def test_a_failed_sign_in_message_does_not_survive_a_refresh(settings: Settings) -> None:
    """Regression: the failure was stored on the authenticator and re-rendered on
    every GET, so /setup kept reporting a sign-in failure indefinitely."""
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("bad password"))
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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


def test_a_failed_mfa_message_does_not_survive_a_refresh(settings: Settings) -> None:
    factory = RecordingFactory(needs_mfa=True, mfa_code="654321")
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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


def test_status_endpoint_reports_the_error_without_consuming_it(settings: Settings) -> None:
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("bad password"))
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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


def test_the_awaiting_mfa_prompt_is_not_a_flash(settings: Settings) -> None:
    """State-derived guidance must persist across refreshes, unlike an error."""
    factory = RecordingFactory(needs_mfa=True)
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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


def test_mfa_form_declares_an_in_flight_message(settings: Settings) -> None:
    factory = RecordingFactory(needs_mfa=True)
    app = create_app(
        settings=settings, authenticator=GarminAuthenticator(settings, garmin_factory=factory)
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
    settings: Settings, ingest: FakeIngest | None = None
) -> tuple[TestClient, FakeIngest]:
    """A TestClient whose account is already linked, with a fake ingest behind it."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text('{"di_refresh_token": "r"}')
    ingest = ingest or FakeIngest()
    app = create_app(
        settings=settings,
        authenticator=GarminAuthenticator(
            settings, garmin_factory=RecordingFactory(needs_mfa=False)
        ),
        ingest_factory=lambda: ingest,
    )
    return TestClient(app=app), ingest


class TestSyncEndpoints:
    @pytest.mark.parametrize(("method", "path"), [("POST", "/sync"), ("GET", "/sync/status")])
    def test_sync_surface_is_owner_gated(self, settings: Settings, method: str, path: str) -> None:
        client, _ = linked_client(settings)
        with client:
            assert client.request(method, path).status_code == 401

    def test_status_reports_link_state_and_interval(self, settings: Settings) -> None:
        client, _ = linked_client(settings)
        with client:
            body = client.get("/sync/status", headers=OWNER).json()
        assert body["link_state"] == "linked"
        assert body["running"] is False
        assert body["last_sync"] is None
        assert body["interval_seconds"] > 0

    def test_trigger_accepts_and_reports_that_it_started(self, settings: Settings) -> None:
        client, ingest = linked_client(settings)
        with client:
            response = client.post("/sync", headers=OWNER)
            assert response.status_code == 202
            assert response.json()["started"] is True
            assert wait_for_sync(client)["last_sync"] is not None
        assert ingest.calls.count("download") == 1

    def test_trigger_is_refused_while_the_account_is_unlinked(self, settings: Settings) -> None:
        """Syncing needs the saved token; silently doing nothing would look like a
        working sync that never produces data."""
        app = create_app(
            settings=settings,
            authenticator=GarminAuthenticator(
                settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
            ingest_factory=lambda: FakeIngest(),
        )
        with TestClient(app=app) as client:
            response = client.post("/sync", headers=OWNER)
            assert response.status_code == 409
            assert "link" in response.json()["detail"].lower()

    def test_status_surfaces_a_failed_sync(self, settings: Settings) -> None:
        client, _ = linked_client(settings, FakeIngest(fail_on="download"))
        with client:
            client.post("/sync", headers=OWNER)
            last = wait_for_sync(client)["last_sync"]
        assert last["error"] is not None
        assert last["phase"] == "download"

    def test_status_surfaces_a_sync_that_changed_nothing(self, settings: Settings) -> None:
        """The signature of GarminDB's importers swallowing every per-file error."""
        same = {"sleep": TableStat(rows=2, latest="2026-06-14T23:00:00")}
        client, _ = linked_client(settings, FakeIngest(stats_sequence=[same, same]))
        with client:
            client.post("/sync", headers=OWNER)
            last = wait_for_sync(client)["last_sync"]
        assert last["error"] is None
        assert last["changed"] is False
        assert last["tables"]["sleep"]["rows"] == 2

    def test_health_stays_ok_while_a_sync_is_failing(self, settings: Settings) -> None:
        """Failing the probe would make the router restart a container whose only
        problem is that Garmin is unreachable."""
        client, _ = linked_client(settings, FakeIngest(fail_on="download"))
        with client:
            client.post("/sync", headers=OWNER)
            assert client.get("/health").status_code == 200


class TestSetupShowsSyncState:
    def test_setup_offers_a_sync_button_once_linked(self, settings: Settings) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "/sync" in page
        assert "sync" in page.lower()

    def test_setup_reports_the_last_sync(self, settings: Settings) -> None:
        client, _ = linked_client(settings)
        with client:
            client.post("/sync", headers=OWNER)
            wait_for_sync(client)
            page = client.get("/setup", headers=OWNER).text
        assert "Last sync" in page

    def test_setup_says_when_nothing_has_synced_yet(self, settings: Settings) -> None:
        client, _ = linked_client(settings)
        with client:
            page = client.get("/setup", headers=OWNER).text
        assert "not synced yet" in page.lower()

    def test_an_unlinked_setup_page_offers_no_sync_button(self, settings: Settings) -> None:
        app = create_app(
            settings=settings,
            authenticator=GarminAuthenticator(
                settings, garmin_factory=RecordingFactory(needs_mfa=False)
            ),
            ingest_factory=lambda: FakeIngest(),
        )
        with TestClient(app=app) as client:
            assert "action='/sync'" not in client.get("/setup", headers=OWNER).text
