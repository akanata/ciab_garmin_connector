"""A stand-in for garminconnect.Garmin that mirrors the real object's contracts.

The behaviours reproduced here are the ones auth.py has to code around, all read
off garminconnect 0.3.11:

- ``return_on_mfa`` is a CONSTRUCTOR argument, not a ``login()`` argument.
- In ``return_on_mfa`` mode ``login()`` returns early and NEVER dumps tokens, and
  never sets ``client._tokenstore_path`` -- so nothing persists the token unless
  the caller does it (``__init__.py:734-741``).
- ``resume_login()`` does not dump tokens either (``__init__.py:884``).
- ``client.resume_login(_client_state, mfa_code)`` IGNORES client_state; the
  pending challenge lives on the instance (``client.py:1608``).
- ``client.resume_login`` clears the pending-MFA state in a ``finally``, so a
  rejected code kills the challenge -- it cannot be retried on the same client.
"""

import json
import threading
from pathlib import Path
from typing import Any

from garminconnect import GarminConnectAuthenticationError

from garmin_health.sync import TableStat


class FakeInnerClient:
    """Stands in for ``Garmin.client`` (the garth-like transport)."""

    def __init__(self) -> None:
        self.dumped_to: list[str] = []
        self._tokenstore_path: str | None = None

    def dump(self, path: str) -> None:
        self.dumped_to.append(path)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}))


class FakeGarmin:
    """Stands in for ``garminconnect.Garmin``."""

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        is_cn: bool = False,
        return_on_mfa: bool = False,
        *,
        needs_mfa: bool = False,
        mfa_code: str = "123456",
        login_error: Exception | None = None,
        **_: Any,
    ) -> None:
        self.username = email
        self.password = password
        self.is_cn = is_cn
        self.return_on_mfa = return_on_mfa
        self.client = FakeInnerClient()
        self.display_name = "rider"
        self.full_name = "Test Rider"
        self._needs_mfa = needs_mfa
        self._expected_code = mfa_code
        self._login_error = login_error
        self._mfa_pending = False
        self.login_calls: list[str | None] = []
        self.resume_calls: list[tuple[Any, str]] = []

    def login(self, tokenstore: str | None = None) -> tuple[str | None, str | None]:
        self.login_calls.append(tokenstore)
        if tokenstore and Path(tokenstore).is_file():
            return None, None  # cached-token path
        if self._login_error is not None:
            raise self._login_error
        if not self.username or not self.password:
            raise GarminConnectAuthenticationError("Username and password are required")
        if self._needs_mfa and self.return_on_mfa:
            self._mfa_pending = True
            # Real client returns early WITHOUT dumping tokens.
            return "needs_mfa", None
        return None, None

    def resume_login(self, client_state: Any, mfa_code: str) -> tuple[Any, Any]:
        self.resume_calls.append((client_state, mfa_code))
        try:
            if not self._mfa_pending:
                raise GarminConnectAuthenticationError("no MFA login in progress")
            if mfa_code != self._expected_code:
                raise GarminConnectAuthenticationError("invalid MFA code")
            return None, None
        finally:
            # Mirrors client.py's finally block: the challenge is consumed either way.
            self._mfa_pending = False


class RecordingFactory:
    """Captures the kwargs auth.py passes, so the constructor contract is testable."""

    def __init__(self, **client_kwargs: Any) -> None:
        self.client_kwargs = client_kwargs
        self.calls: list[dict[str, Any]] = []
        self.clients: list[FakeGarmin] = []

    def __call__(self, **kwargs: Any) -> FakeGarmin:
        self.calls.append(kwargs)
        client = FakeGarmin(**{**kwargs, **self.client_kwargs})
        self.clients.append(client)
        return client


class FakeIngest:
    """Stands in for garmin/ingest.py: records the sequence without touching GarminDB."""

    def __init__(
        self,
        *,
        stats_sequence: list[dict[str, Any]] | None = None,
        fail_on: str | None = None,
        block_on: Any | None = None,
    ) -> None:
        default = {"sleep": TableStat(rows=1, latest="2026-06-14T23:00:00")}
        self._stats_sequence = stats_sequence or [default, default]
        self._fail_on = fail_on
        self._block_on = block_on
        self.calls: list[str] = []
        self.thread_ids: list[int] = []
        self.stop_event: Any | None = None

    def _record(self, name: str) -> None:
        self.calls.append(name)
        self.thread_ids.append(threading.get_ident())
        if self._fail_on == name:
            raise RuntimeError(f"boom in {name}")

    def table_stats(self) -> dict[str, Any]:
        self.calls.append("table_stats")
        self.thread_ids.append(threading.get_ident())
        seen = self.calls.count("table_stats")
        if self._fail_on == "table_stats_after" and seen > 1:
            raise RuntimeError("boom reading stats")
        index = min(seen - 1, len(self._stats_sequence) - 1)
        return dict(self._stats_sequence[index])

    def download(self, stop: Any) -> None:
        self.stop_event = stop
        if self._block_on is not None:
            self._block_on.wait(5)
        self._record("download")

    def import_(self, stop: Any) -> None:
        self.stop_event = stop
        self._record("import")

    def analyze(self) -> None:
        self._record("analyze")

    def rebuild(self, stop: Any) -> None:
        self.stop_event = stop
        self._record("rebuild")
