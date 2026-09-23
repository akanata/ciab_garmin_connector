import json
import stat

import pytest
from garminconnect import GarminConnectAuthenticationError

from garmin_health.ports import LinkState
from garmin_health.providers.garmindb.auth import AuthError
from garmin_health.providers.garmindb.auth import GarminAuthenticator
from garmin_health.providers.garmindb.config_file import config_user
from garmin_health.providers.garmindb.settings import GarminDbSettings
from tests.providers.garmindb.fakes import RecordingFactory


def make_auth(
    settings: GarminDbSettings, **client_kwargs: object
) -> tuple[GarminAuthenticator, RecordingFactory]:
    factory = RecordingFactory(**client_kwargs)
    return GarminAuthenticator(settings, garmin_factory=factory), factory


async def test_starts_not_linked_with_no_token_on_disk(settings: GarminDbSettings) -> None:
    auth, _ = make_auth(settings)
    assert auth.status().state is LinkState.NOT_LINKED


async def test_an_existing_token_file_means_already_linked(settings: GarminDbSettings) -> None:
    """The steady state after a restart: the token outlives the process."""
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.token_file.write_text(json.dumps({"di_refresh_token": "r"}))
    auth, _ = make_auth(settings)
    assert auth.status().state is LinkState.LINKED


async def test_login_without_mfa_links_immediately(settings: GarminDbSettings) -> None:
    auth, _ = make_auth(settings, needs_mfa=False)
    status = await auth.login("rider@example.com", "hunter2")
    assert status.state is LinkState.LINKED
    assert status.account == "rider@example.com"


async def test_login_passes_return_on_mfa_to_the_constructor(settings: GarminDbSettings) -> None:
    """garminconnect takes return_on_mfa on __init__, NOT on login(). Passing it to
    login() would be a TypeError; omitting it entirely would fall through to
    prompt_mfa's blocking input() on stdin, which is fatal in a container."""
    auth, factory = make_auth(settings, needs_mfa=True)
    await auth.login("rider@example.com", "hunter2")
    assert factory.calls[-1]["return_on_mfa"] is True
    assert factory.calls[-1]["email"] == "rider@example.com"
    assert "prompt_mfa" not in factory.calls[-1]


async def test_login_persists_the_token_itself(settings: GarminDbSettings) -> None:
    """In return_on_mfa mode garminconnect's login() returns before it would ever
    set _tokenstore_path or dump(), so nothing writes the token unless we do."""
    auth, factory = make_auth(settings, needs_mfa=False)
    await auth.login("rider@example.com", "hunter2")
    assert factory.clients[-1].client.dumped_to == [str(settings.token_file)]
    assert settings.token_file.is_file()


async def test_mfa_challenge_moves_to_pending_and_does_not_write_a_token(
    settings: GarminDbSettings,
) -> None:
    auth, factory = make_auth(settings, needs_mfa=True)
    status = await auth.login("rider@example.com", "hunter2")
    assert status.state is LinkState.PENDING
    assert factory.clients[-1].client.dumped_to == []
    assert not settings.token_file.exists()


async def test_completing_mfa_links_and_writes_the_token(settings: GarminDbSettings) -> None:
    auth, factory = make_auth(settings, needs_mfa=True, mfa_code="654321")
    await auth.login("rider@example.com", "hunter2")
    status = await auth.complete_mfa("654321")
    assert status.state is LinkState.LINKED
    assert settings.token_file.is_file()
    assert factory.clients[-1].client.dumped_to == [str(settings.token_file)]


async def test_mfa_resumes_on_the_same_client_instance(settings: GarminDbSettings) -> None:
    """resume_login's client_state argument is ignored (client.py takes _client_state),
    so the pending challenge only exists on the retained instance."""
    auth, factory = make_auth(settings, needs_mfa=True)
    await auth.login("rider@example.com", "hunter2")
    await auth.complete_mfa("123456")
    assert len(factory.clients) == 1
    assert factory.clients[0].resume_calls == [({}, "123456")]


async def test_wrong_mfa_code_requires_starting_over(settings: GarminDbSettings) -> None:
    """client.resume_login clears the pending-MFA state in a finally block, so a
    rejected code consumes the challenge -- the owner must re-enter credentials
    rather than retry the code against a client that can no longer accept one."""
    auth, _ = make_auth(settings, needs_mfa=True, mfa_code="654321")
    await auth.login("rider@example.com", "hunter2")
    with pytest.raises(AuthError):
        await auth.complete_mfa("000000")
    assert auth.status().state is LinkState.NOT_LINKED
    assert not settings.token_file.exists()


async def test_mfa_without_a_pending_challenge_is_rejected(settings: GarminDbSettings) -> None:
    auth, _ = make_auth(settings)
    with pytest.raises(AuthError):
        await auth.complete_mfa("123456")


async def test_bad_credentials_surface_as_auth_error_and_stay_unlinked(
    settings: GarminDbSettings,
) -> None:
    auth, _ = make_auth(settings, login_error=GarminConnectAuthenticationError("bad password"))
    with pytest.raises(AuthError):
        await auth.login("rider@example.com", "wrong")
    assert auth.status().state is LinkState.NOT_LINKED
    assert not settings.token_file.exists()


async def test_login_never_writes_the_password_to_disk(settings: GarminDbSettings) -> None:
    """There is no OAuth consent flow for this API, so the app handles the owner's
    real Garmin password. It must not outlive the request."""
    auth, _ = make_auth(settings, needs_mfa=False)
    await auth.login("rider@example.com", "hunter2")
    for path in settings.app_data_dir.rglob("*"):
        if path.is_file():
            assert "hunter2" not in path.read_text()


async def test_login_records_the_user_in_the_garmindb_config(settings: GarminDbSettings) -> None:
    auth, _ = make_auth(settings, needs_mfa=False)
    await auth.login("rider@example.com", "hunter2")
    assert config_user(settings) == "rider@example.com"


async def test_pending_password_is_dropped_once_mfa_completes(settings: GarminDbSettings) -> None:
    auth, _ = make_auth(settings, needs_mfa=True)
    await auth.login("rider@example.com", "hunter2")
    await auth.complete_mfa("123456")
    assert auth._pending is None


async def test_unlink_removes_the_token_and_returns_to_not_linked(
    settings: GarminDbSettings,
) -> None:
    auth, _ = make_auth(settings, needs_mfa=False)
    await auth.login("rider@example.com", "hunter2")
    status = await auth.unlink()
    assert status.state is LinkState.NOT_LINKED
    assert not settings.token_file.exists()


async def test_a_deleted_token_flips_a_linked_session_to_needs_reauth(
    settings: GarminDbSettings,
) -> None:
    """The sync loop must not keep believing it is linked after the token is gone."""
    auth, _ = make_auth(settings, needs_mfa=False)
    await auth.login("rider@example.com", "hunter2")
    settings.token_file.unlink()
    assert auth.status().state is LinkState.NEEDS_REAUTH


async def test_status_never_exposes_the_password(settings: GarminDbSettings) -> None:
    auth, _ = make_auth(settings, needs_mfa=True)
    await auth.login("rider@example.com", "hunter2")
    assert "hunter2" not in json.dumps(auth.status().as_dict())


async def test_a_failure_message_is_reported_once_and_then_cleared(
    settings: GarminDbSettings,
) -> None:
    """The error is a one-shot flash. Left sticky, it would still be accusing the
    owner of a bad password on every later visit to /setup."""
    auth, _ = make_auth(settings, login_error=GarminConnectAuthenticationError("bad password"))
    with pytest.raises(AuthError):
        await auth.login("rider@example.com", "wrong")
    first = auth.take_error()
    assert first is not None and "failed" in first.lower()
    assert auth.take_error() is None


async def test_peeking_at_the_error_does_not_consume_it(settings: GarminDbSettings) -> None:
    """/setup/status is pollable, so reading it must not eat the flash that /setup
    is about to render."""
    auth, _ = make_auth(settings, login_error=GarminConnectAuthenticationError("bad password"))
    with pytest.raises(AuthError):
        await auth.login("rider@example.com", "wrong")
    assert auth.peek_error() is not None
    assert auth.peek_error() is not None
    assert auth.take_error() is not None


async def test_status_detail_never_carries_a_failure(settings: GarminDbSettings) -> None:
    """detail is state-derived guidance and must stay idempotent; errors travel
    separately so they can be consumed."""
    auth, _ = make_auth(settings, login_error=GarminConnectAuthenticationError("bad password"))
    with pytest.raises(AuthError):
        await auth.login("rider@example.com", "wrong")
    assert auth.status().detail is None


async def test_a_later_success_clears_an_earlier_failure(settings: GarminDbSettings) -> None:
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("bad password"))
    auth = GarminAuthenticator(settings, garmin_factory=factory)
    with pytest.raises(AuthError):
        await auth.login("rider@example.com", "wrong")
    factory.client_kwargs.pop("login_error")
    await auth.login("rider@example.com", "hunter2")
    assert auth.peek_error() is None


async def test_pending_detail_survives_being_read_twice(settings: GarminDbSettings) -> None:
    auth, _ = make_auth(settings, needs_mfa=True)
    await auth.login("rider@example.com", "hunter2")
    assert auth.status().detail == auth.status().detail
    assert auth.status().detail is not None


VALID_TOKEN = json.dumps(
    {"di_token": "access", "di_refresh_token": "refresh", "di_client_id": "client"}
)


class TestTokenImport:
    """The escape hatch for a Cloudflare bot challenge on the sign-in portal.

    The challenge guards only the interactive credential exchange on
    sso.garmin.com. Refresh (diauth.garmin.com) and data (connectapi.garmin.com)
    are off that path -- garminconnect refreshes proactively precisely to avoid
    the blocked endpoint -- so a token minted anywhere links the app for good.
    """

    async def test_a_valid_token_links_the_account(self, settings: GarminDbSettings) -> None:
        factory = RecordingFactory()
        auth = GarminAuthenticator(settings, garmin_factory=factory)
        result = await auth.link_with_token(VALID_TOKEN, email="rider@example.com")
        assert result.state is LinkState.LINKED
        assert settings.token_file.is_file()

    async def test_it_never_touches_the_sign_in_portal(self, settings: GarminDbSettings) -> None:
        """The whole point: no credentials, no SSO round trip, no challenge."""
        factory = RecordingFactory()
        auth = GarminAuthenticator(settings, garmin_factory=factory)
        await auth.link_with_token(VALID_TOKEN, email="")

        client = factory.clients[0]
        assert client.token_logins == [VALID_TOKEN]
        assert client.password is None
        assert factory.calls[0].get("password") is None

    async def test_the_persisted_file_is_what_the_client_holds(
        self, settings: GarminDbSettings
    ) -> None:
        """Written through the client's own dump(), not by copying the pasted
        bytes: dump() normalises and picks up a proactive refresh."""
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory())
        await auth.link_with_token(VALID_TOKEN, email="")
        assert json.loads(settings.token_file.read_text())["di_refresh_token"] == "refresh"

    async def test_the_token_file_is_owner_only(self, settings: GarminDbSettings) -> None:
        """It is a bearer credential granting persistent account access."""
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory())
        await auth.link_with_token(VALID_TOKEN, email="")
        assert stat.S_IMODE(settings.token_file.stat().st_mode) == 0o600

    async def test_the_email_is_recorded_for_the_config(self, settings: GarminDbSettings) -> None:
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory())
        await auth.link_with_token(VALID_TOKEN, email="rider@example.com")
        assert auth.status().account == "rider@example.com"
        assert config_user(settings) == "rider@example.com"

    async def test_the_email_is_optional(self, settings: GarminDbSettings) -> None:
        """A token carries no email, and the account links perfectly well without
        one -- it only labels the page and GarminDB's unusable credential fallback."""
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory())
        assert (await auth.link_with_token(VALID_TOKEN, email="")).state is LinkState.LINKED

    @pytest.mark.parametrize(
        "raw", ["", "   ", "not json", "[]", '"a string"', "{}", '{"di_token": "t"}']
    )
    async def test_a_malformed_token_is_refused_before_any_network_call(
        self, settings: GarminDbSettings, raw: str
    ) -> None:
        """Shape-checked first: a paste that lost a brace should say so, not come
        back as an opaque authentication failure."""
        factory = RecordingFactory()
        auth = GarminAuthenticator(settings, garmin_factory=factory)
        with pytest.raises(AuthError):
            await auth.link_with_token(raw, email="")
        assert factory.clients == []
        assert not settings.token_file.exists()

    async def test_a_token_the_api_rejects_leaves_nothing_behind(
        self, settings: GarminDbSettings
    ) -> None:
        """A persisted-but-dead token is the exact "looks linked, every sync
        fails" trap this codebase exists to avoid."""
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory(token_rejected=True))
        with pytest.raises(AuthError):
            await auth.link_with_token(VALID_TOKEN, email="")
        assert not settings.token_file.exists()
        assert auth.status().state is LinkState.NOT_LINKED

    async def test_a_rejection_is_reported_as_a_one_shot_flash(
        self, settings: GarminDbSettings
    ) -> None:
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory(token_rejected=True))
        with pytest.raises(AuthError):
            await auth.link_with_token(VALID_TOKEN, email="")
        assert auth.take_error() is not None
        assert auth.take_error() is None

    async def test_it_relinks_an_account_that_needs_reauth(
        self, settings: GarminDbSettings
    ) -> None:
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory())
        await auth.link_with_token(VALID_TOKEN, email="")
        settings.token_file.unlink()
        assert auth.status().state is LinkState.NEEDS_REAUTH

        await auth.link_with_token(VALID_TOKEN, email="")
        assert auth.status().state is LinkState.LINKED

    async def test_it_clears_a_half_finished_mfa_attempt(self, settings: GarminDbSettings) -> None:
        """Importing a token is a complete alternative to the credential flow, so
        a challenge left over from a failed sign-in must not outlive it."""
        auth = GarminAuthenticator(settings, garmin_factory=RecordingFactory(needs_mfa=True))
        await auth.login("rider@example.com", "hunter2")
        assert auth.status().state is LinkState.PENDING

        await auth.link_with_token(VALID_TOKEN, email="")
        assert auth.status().state is LinkState.LINKED
