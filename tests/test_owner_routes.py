"""The owner page's shell: the guard, the headline, the flash, and unlink.

Mounted over a :class:`FakeProvider`, because none of these rules are a
provider's. GarminDB's own half of the page -- credentials, MFA, the token
import, the import scope and the coverage table -- is in
``tests/providers/garmindb/test_owner_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from litestar.testing import TestClient

from garmin_health.app import create_app
from garmin_health.ports import LinkState
from tests.fakes import FakeLink
from tests.fakes import FakeProvider
from tests.fakes import FakeReader

OWNER = {"X-OpenHost-Is-Owner": "true"}


def client_for(provider: FakeProvider) -> Iterator[TestClient]:
    with TestClient(app=create_app(provider=provider)) as c:
        yield c


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(provider: FakeProvider) -> Iterator[TestClient]:
    yield from client_for(provider)


class TestGuard:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/"),
            ("GET", "/setup"),
            ("GET", "/setup/status"),
            ("POST", "/setup/unlink"),
            ("GET", "/sync/status"),
        ],
    )
    def test_the_whole_owner_surface_is_gated(
        self, client: TestClient, method: str, path: str
    ) -> None:
        assert client.request(method, path).status_code == 401

    @pytest.mark.parametrize("value", ["false", "1", "TRUE", ""])
    def test_a_forged_owner_header_value_is_not_accepted(
        self, client: TestClient, value: str
    ) -> None:
        """The router stamps exactly "true"; anything else is a client trying it on."""
        assert client.get("/setup", headers={"X-OpenHost-Is-Owner": value}).status_code == 401

    def test_health_needs_no_owner_header(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200


class TestHeadline:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (LinkState.NOT_LINKED, "Not linked to Fake Health yet."),
            (LinkState.PENDING, "Finishing the link to Fake Health."),
            (LinkState.NEEDS_REAUTH, "This bottle is no longer linked to Fake Health."),
        ],
    )
    def test_each_state_has_its_own_headline(self, state: LinkState, expected: str) -> None:
        provider = FakeProvider(link=FakeLink(state=state))
        for client in client_for(provider):
            page = client.get("/setup", headers=OWNER).text
            assert expected in page
            assert f"data-state='{state.value}'" in page

    def test_a_linked_account_is_named(self) -> None:
        provider = FakeProvider(link=FakeLink(state=LinkState.LINKED, account="rider@example.com"))
        for client in client_for(provider):
            assert (
                "Linked to Fake Health as rider@example.com."
                in client.get("/setup", headers=OWNER).text
            )

    def test_a_linked_account_with_no_name_still_reads_properly(self) -> None:
        """A provider that links by token has no account name to show."""
        provider = FakeProvider(link=FakeLink(state=LinkState.LINKED))
        for client in client_for(provider):
            assert "Linked to Fake Health." in client.get("/setup", headers=OWNER).text

    def test_the_state_detail_is_rendered(self) -> None:
        provider = FakeProvider(
            link=FakeLink(state=LinkState.PENDING, detail="Enter the code we sent you.")
        )
        for client in client_for(provider):
            assert "Enter the code we sent you." in client.get("/setup", headers=OWNER).text

    def test_the_detail_is_not_a_flash(self) -> None:
        """State-derived guidance must persist across refreshes, unlike an error."""
        provider = FakeProvider(link=FakeLink(state=LinkState.PENDING, detail="Enter the code."))
        for client in client_for(provider):
            assert "Enter the code." in client.get("/setup", headers=OWNER).text
            assert "Enter the code." in client.get("/setup", headers=OWNER).text


class TestProviderFragment:
    def test_the_providers_markup_is_embedded_in_the_shell(self, client: TestClient) -> None:
        page = client.get("/setup", headers=OWNER).text
        assert "id='fake-setup'" in page
        assert "<!doctype html>" in page
        assert "</body></html>" in page

    def test_the_provider_names_the_page(self, client: TestClient) -> None:
        page = client.get("/setup", headers=OWNER).text
        assert "<title>Fake Health setup</title>" in page
        assert "<h1>Fake Health</h1>" in page

    def test_the_serving_fault_reaches_the_provider(self) -> None:
        """Serving health and acquisition health fail independently, and the fix
        for a degraded store is usually the provider's to offer."""
        provider = FakeProvider(reader=FakeReader(fault="The schema on disk must be rebuilt."))
        for client in client_for(provider):
            page = client.get("/setup", headers=OWNER).text
            assert "data-fault='The schema on disk must be rebuilt.'" in page
            assert provider.setup_views[-1].serving_fault == "The schema on disk must be rebuilt."

    def test_the_bare_root_renders_the_same_page(self, client: TestClient) -> None:
        assert client.get("/", headers=OWNER).text == client.get("/setup", headers=OWNER).text


class TestFlash:
    def test_the_page_renders_the_error_and_consumes_it(self) -> None:
        """One mistyped password must not accuse the owner for ever."""
        link = FakeLink()
        link.plant_error("Sign-in failed: bad password.")
        for client in client_for(FakeProvider(link=link)):
            assert "Sign-in failed: bad password." in client.get("/setup", headers=OWNER).text
            assert "Sign-in failed" not in client.get("/setup", headers=OWNER).text

    def test_the_status_endpoint_only_peeks(self) -> None:
        """/setup/status is pollable; consuming the flash there would steal the
        message /setup still has to render."""
        link = FakeLink()
        link.plant_error("Sign-in failed: bad password.")
        for client in client_for(FakeProvider(link=link)):
            assert client.get("/setup/status", headers=OWNER).json()["error"] is not None
            assert client.get("/setup/status", headers=OWNER).json()["error"] is not None
            assert "Sign-in failed" in client.get("/setup", headers=OWNER).text
            assert client.get("/setup/status", headers=OWNER).json()["error"] is None


class TestSetupStatus:
    def test_it_reports_the_link_state_in_the_ports_vocabulary(self) -> None:
        provider = FakeProvider(link=FakeLink(state=LinkState.PENDING, account="rider"))
        for client in client_for(provider):
            body = client.get("/setup/status", headers=OWNER).json()
        assert body == {"state": "pending", "account": "rider", "detail": None, "error": None}


class TestUnlink:
    def test_it_asks_the_provider_to_unlink_and_redirects(self) -> None:
        link = FakeLink(state=LinkState.LINKED, account="rider@example.com")
        provider = FakeProvider(link=link)
        for client in client_for(provider):
            response = client.post("/setup/unlink", headers=OWNER, follow_redirects=False)
            assert response.status_code == 303
            assert link.unlink_calls == 1
            assert client.get("/setup/status", headers=OWNER).json()["state"] == "not_linked"
