from collections.abc import Iterator

import pytest
from garminconnect import GarminConnectAuthenticationError
from litestar.testing import TestClient

from garmin_health.app import create_app
from garmin_health.auth import GarminAuthenticator
from garmin_health.config import Settings
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
