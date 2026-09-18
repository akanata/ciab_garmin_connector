"""Garmin Connect login and the MFA state machine.

There is no OAuth consent flow for this API. ``garminconnect`` authenticates by
replaying the owner's real Garmin password against Garmin's SSO web form and
exchanging the resulting service ticket for OAuth2 tokens; those tokens are the
*output* of the exchange, not the mechanism. Garmin's actual OAuth API sits behind
the developer portal, which is the reason this project uses GarminDB at all.

Three contracts of garminconnect 0.3.11 shape everything here:

1. ``return_on_mfa`` is a CONSTRUCTOR argument (``__init__.py:371``), not a
   ``login()`` argument. Without it, login falls through to ``prompt_mfa``, whose
   default is a blocking ``input()`` on stdin -- fatal in a container.
2. In that mode ``login()`` returns early (``__init__.py:734``) without setting
   ``client._tokenstore_path`` and without dumping, and ``resume_login()`` does not
   dump either. **Nothing persists the token unless we do it.** A login that looks
   successful but writes no token would make every later sync re-prompt for MFA.
3. ``client.resume_login`` ignores its ``client_state`` argument (``client.py:1608``
   takes ``_client_state``): the pending challenge lives on the ``Garmin``
   instance, so that object must be retained between the two requests. It also
   clears the pending state in a ``finally``, so a rejected code consumes the
   challenge and the flow has to restart.

The login attempt does not survive a process restart; the token does.
"""

from __future__ import annotations

import json
import logging
from threading import Lock
from typing import Any
from typing import Protocol

import anyio.to_thread
import attrs
from garminconnect import Garmin

from garmin_health.ports import LinkState
from garmin_health.ports import LinkStatus
from garmin_health.providers.garmindb.config_file import config_user
from garmin_health.providers.garmindb.config_file import ensure_config
from garmin_health.providers.garmindb.settings import GarminDbSettings

logger = logging.getLogger(__name__)

NEEDS_MFA = "needs_mfa"

# What garminconnect's own dump() writes, and what GarminDB later reads. Only the
# refresh token is load-bearing -- it is what mints new access tokens -- but a
# paste missing any of them is a truncated copy rather than a usable token.
TOKEN_FIELDS = ("di_token", "di_refresh_token", "di_client_id")


class AuthError(Exception):
    """The owner-facing reason a link attempt did not succeed."""


class GarminFactory(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


@attrs.define
class _PendingLogin:
    """A half-finished MFA challenge, alive only in this process's memory."""

    client: Any
    email: str


def _default_garmin_factory(**kwargs: Any) -> Garmin:
    return Garmin(**kwargs)


class GarminAuthenticator:
    """Owns the link state and the two-step MFA login."""

    def __init__(
        self, settings: GarminDbSettings, garmin_factory: GarminFactory | None = None
    ) -> None:
        self._settings = settings
        self._factory: GarminFactory = garmin_factory or _default_garmin_factory
        self._lock = Lock()
        self._pending: _PendingLogin | None = None
        self._email: str | None = config_user(settings)
        # A one-shot flash, not part of the status. Stored on the authenticator it
        # would be re-rendered on every later GET of /setup, so a single mistyped
        # password would keep accusing the owner indefinitely.
        self._error: str | None = None
        # Remembering that a token once existed is what distinguishes "never set up"
        # from "the token went away and the owner has to sign in again".
        self._was_linked = settings.token_file.is_file()

    def status(self) -> LinkStatus:
        """The link state and the guidance that follows from it.

        ``detail`` is derived from the state alone, so reading it is idempotent.
        Failures are not status -- they are a flash, read with take_error().
        """
        if self._settings.token_file.is_file():
            return LinkStatus(LinkState.LINKED, self._email)
        if self._pending is not None:
            # PENDING is the generic state; the MFA wording is what makes it
            # actionable, and detail is where a provider says so.
            return LinkStatus(
                LinkState.PENDING,
                self._pending.email,
                "Enter the code Garmin just sent you to finish linking.",
            )
        if self._was_linked:
            return LinkStatus(
                LinkState.NEEDS_REAUTH,
                self._email,
                "The saved Garmin token is no longer on disk. Sign in again to relink.",
            )
        return LinkStatus(LinkState.NOT_LINKED, self._email)

    def peek_error(self) -> str | None:
        """Read the pending failure without clearing it (for pollable endpoints)."""
        return self._error

    def take_error(self) -> str | None:
        """Read and clear the pending failure, so a refresh does not repeat it."""
        error, self._error = self._error, None
        return error

    async def login(self, email: str, password: str) -> LinkStatus:
        """Start a login. GarminDB and garminconnect are entirely synchronous, so
        this must never run on the event loop."""
        return await anyio.to_thread.run_sync(self._login_sync, email, password)

    async def complete_mfa(self, code: str) -> LinkStatus:
        return await anyio.to_thread.run_sync(self._complete_mfa_sync, code)

    async def link_with_token(self, raw: str, *, email: str = "") -> LinkStatus:
        """Link using a token minted elsewhere, bypassing the sign-in portal.

        Garmin's SSO portal sits behind a Cloudflare bot challenge that a
        container on a datacenter IP frequently cannot pass. That challenge guards
        only the interactive credential exchange: refresh runs against
        ``diauth.garmin.com`` and data against ``connectapi.garmin.com``, and
        garminconnect refreshes proactively *precisely* to avoid the blocked
        endpoint (``__init__.py:703``). GarminDB's own adapter tries the token
        store before credentials too.

        So a token minted once on any machine that can sign in -- a laptop on a
        residential connection -- links this app for the life of the refresh token,
        and nothing here ever has to pass a challenge.
        """
        return await anyio.to_thread.run_sync(self._link_with_token_sync, raw, email)

    async def unlink(self) -> LinkStatus:
        return await anyio.to_thread.run_sync(self._unlink_sync)

    def _fail(self, message: str) -> AuthError:
        self._error = message
        return AuthError(message)

    def _persist_tokens(self, client: Any) -> None:
        """Write garmin_tokens.json ourselves -- garminconnect will not, in this mode.

        The path must be exactly GarminConnectConfigManager.get_token_store_file(),
        or GarminDB would never find the token and every sync would fall back to a
        credential login that has no password on disk.
        """
        target = str(self._settings.token_file)
        try:
            client.client.dump(target)
        except Exception as exc:
            raise self._fail(f"Signed in to Garmin but could not save the token: {exc}") from exc
        if not self._settings.token_file.is_file():
            raise self._fail("Signed in to Garmin but the token file was not written.")

    def _login_sync(self, email: str, password: str) -> LinkStatus:
        email = (email or "").strip()
        if not email or not password:
            raise self._fail("Enter both your Garmin Connect email and password.")

        with self._lock:
            self._pending = None
            self._error = None
            ensure_config(self._settings, user=email)

            client = self._factory(
                email=email,
                password=password,
                is_cn=self._settings.is_cn,
                return_on_mfa=True,
            )
            try:
                mfa_status, _ = client.login(str(self._settings.token_file))
            except Exception as exc:
                logger.warning("Garmin sign-in failed for the configured account: %s", exc)
                raise self._fail(f"Garmin sign-in failed: {exc}") from exc

            if mfa_status == NEEDS_MFA:
                self._pending = _PendingLogin(client=client, email=email)
                self._email = email
                return self.status()

            self._persist_tokens(client)
            self._email = email
            self._was_linked = True
            self._error = None
            return self.status()

    def _complete_mfa_sync(self, code: str) -> LinkStatus:
        code = (code or "").strip()
        with self._lock:
            pending = self._pending
            if pending is None:
                raise self._fail("No Garmin sign-in is waiting for a code. Start again.")
            if not code:
                raise self._fail("Enter the verification code Garmin sent you.")

            try:
                pending.client.resume_login({}, code)
            except Exception as exc:
                # resume_login clears the pending-MFA state in a finally block, so the
                # challenge is spent whether or not the code was right. Retrying the
                # code against this client can never succeed; drop it and start over.
                self._pending = None
                logger.warning("Garmin MFA verification failed: %s", exc)
                raise self._fail(
                    f"That verification code was not accepted ({exc}). Enter your email and password again."
                ) from exc

            self._persist_tokens(pending.client)
            # Drop the client, and with it the last reference to this attempt.
            self._pending = None
            self._email = pending.email
            self._was_linked = True
            self._error = None
            return self.status()

    @staticmethod
    def _validated_token(raw: str) -> str:
        """Check the paste is a whole token document before going near the network.

        Shape first, so a paste that lost a brace says so plainly instead of
        coming back as an opaque authentication failure the owner cannot act on.
        """
        text = (raw or "").strip()
        if not text:
            raise AuthError("Paste the contents of garmin_tokens.json.")
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise AuthError(f"That is not valid JSON: {exc}.") from exc
        if not isinstance(document, dict):
            raise AuthError(
                f"Expected a JSON object with the token fields, got {type(document).__name__}."
            )
        missing = [field for field in TOKEN_FIELDS if not document.get(field)]
        if missing:
            raise AuthError(
                f"The token is missing {', '.join(missing)}. Copy the whole "
                "garmin_tokens.json file, including the outer braces."
            )
        return text

    def _link_with_token_sync(self, raw: str, email: str) -> LinkStatus:
        email = (email or "").strip()
        try:
            token = self._validated_token(raw)
        except AuthError as exc:
            raise self._fail(str(exc)) from exc

        with self._lock:
            # Importing a token is a complete alternative to the credential flow,
            # so any challenge left over from a failed sign-in must not outlive it.
            self._pending = None
            self._error = None
            if email:
                ensure_config(self._settings, user=email)

            client = self._factory(is_cn=self._settings.is_cn)
            try:
                # An inline JSON token store is detected structurally, by a leading
                # brace (__init__.py:182), and takes the load-and-refresh path
                # rather than the portal. Verified before persisting: a token the
                # API rejects, written to disk anyway, is the exact "looks linked
                # but every sync fails" trap this codebase exists to avoid.
                client.login(token)
            except Exception as exc:
                logger.warning("Imported Garmin token was rejected: %s", exc)
                raise self._fail(
                    f"Garmin rejected that token ({exc}). Mint a fresh one and try again."
                ) from exc

            self._persist_tokens(client)
            if email:
                self._email = email
            self._was_linked = True
            self._error = None
            logger.info("Garmin account linked from an imported token.")
            return self.status()

    def _unlink_sync(self) -> LinkStatus:
        with self._lock:
            self._pending = None
            self._settings.token_file.unlink(missing_ok=True)
            self._was_linked = False
            self._error = None
            return self.status()
