"""HTTP client for QuickBooks Online (P8 Task 4).

A deliberately small, synchronous httpx client that speaks exactly the slice
of Intuit's API the push needs: the OAuth2 refresh grant and journal-entry
create. Faithful to the real-Intuit behaviors the mock enforces:

- Token refresh uses HTTP Basic auth (client_id:client_secret) and Intuit
  ROTATES the refresh token on every grant — the rotated token is written
  back through the injected `TokenStore` (the configured one is only the
  bootstrap; losing a rotation locks the client out of the next refresh).
- Every ``/v3/*`` call sends ``Accept: application/json`` (real QBO answers
  in XML otherwise).
- ``post_journal_entry`` retries: a 401 triggers ONE token refresh + retry
  (access tokens expire hourly); a 429 honors ``Retry-After`` for up to
  ``_MAX_ATTEMPTS`` total attempts. Anything else raises ``QboError``.

``SyncASGITransport`` bridges this sync client to the in-process FastAPI mock
for tests: httpx's own ``ASGITransport`` is async-only, so each request runs
on a private ``asyncio`` event loop. That is safe here because the mock keeps
all state on the app object (never on the loop) and its handlers spawn no
background tasks; request/response bodies are fully buffered on both sides.
"""

import asyncio
import base64
import threading
import time
from typing import Any, Protocol

import httpx

_MAX_ATTEMPTS = 3  # total POST attempts when throttled (429)
_TOKEN_PATH = "/oauth2/v1/tokens/bearer"

# Module-level sleep hook so tests can stub the throttle backoff.
_sleep = time.sleep


class QboError(Exception):
    """A QBO call failed after the client's retry budget was exhausted."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"QBO {status}: {message}")
        self.status = status
        self.message = message


class SyncASGITransport(httpx.BaseTransport):
    """Sync httpx transport over an in-process ASGI app (test/dev only).

    Wraps ``httpx.ASGITransport`` (async-only) and drives it with a private
    event loop per request via ``asyncio.run``. Fine for the mock QBO: state
    lives on the app instance, handlers never schedule background work, and
    bodies are small enough to buffer.

    Constraint: must be called from a thread with NO running event loop —
    ``asyncio.run`` raises RuntimeError otherwise. Sync test/CLI code only;
    async callers should use ``httpx.AsyncClient`` with a plain
    ``httpx.ASGITransport`` instead.
    """

    def __init__(self, app: Any) -> None:
        self._asgi = httpx.ASGITransport(app=app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _run() -> tuple[int, httpx.Headers, bytes]:
            response = await self._asgi.handle_async_request(request)
            body = await response.aread()
            await response.aclose()
            return response.status_code, response.headers, body

        status, headers, body = asyncio.run(_run())
        return httpx.Response(status_code=status, headers=headers, content=body, request=request)


def _retry_after_seconds(resp: httpx.Response) -> float:
    """Seconds to back off on a 429; defaults to 1.0 when Retry-After is absent
    or unparseable (RFC 9110 also allows an HTTP-date form we don't decode)."""
    try:
        return float(resp.headers.get("Retry-After", "1"))
    except ValueError:
        return 1.0


def _error_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from either Intuit fault shape."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:200]
    if not isinstance(payload, dict):
        return resp.text[:200]
    fault = payload.get("Fault") or payload.get("fault")
    if isinstance(fault, dict):
        errors = fault.get("Error") or fault.get("error")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            first = errors[0]
            message = str(first.get("Message") or first.get("message") or "").strip()
            detail = str(first.get("Detail") or first.get("detail") or "").strip()
            if message and detail:
                return f"{message}: {detail}"
            if message or detail:
                return message or detail
    if isinstance(payload.get("error"), str):
        return str(payload["error"])
    return resp.text[:200]


class TokenStore(Protocol):
    """Where one tenant's QBO refresh token lives across calls (OH-17,
    D-OH17.7).

    Intuit rotates the refresh token on EVERY grant, so whoever holds it must
    be able to write the new one back. Before OH-17 that holder was process
    memory, which meant a restart lost the rotation and the next push
    invalid_grant'd against a spent token. The DB-backed implementation
    (`usali.integrations.DbTokenStore`) makes the lineage per-tenant and
    durable; `StaticTokenStore` preserves the old in-memory behaviour for the
    mock and for tests."""

    def load(self) -> str: ...
    def store(self, refresh_token: str) -> None: ...


class StaticTokenStore:
    """In-memory token store — dev, tests, and the `usali qbo-mock` loop.
    Rotation survives for the client's lifetime and no longer."""

    def __init__(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token

    def load(self) -> str:
        return self._refresh_token

    def store(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token


class QboClient:
    """Minimal QuickBooks Online client: refresh-grant auth + JE create.

    Refresh-token rotation is written back through a `TokenStore` port, so
    where the lineage lives — and how long it survives — is the caller's
    choice, not this class's. The first refresh invalidates the token the
    store handed out, so two clients sharing a `StaticTokenStore`-style
    in-memory lineage must be one instance for its whole lifetime (the portal
    holds one via an app.state factory), and a SECOND process bootstrapped
    from the same static token gets `invalid_grant`. A durable store
    (`usali.integrations.DbTokenStore`, which takes a row lock) is what makes
    the rotation outlive this process and serialize across them.

    Thread safety: `post_journal_entry` holds an internal lock for its whole
    body (lazy refresh + POST + retry loops), so concurrent callers on one
    shared instance are serialized. Without it, two threads could both see a
    missing access token and refresh with the SAME refresh token — the loser's
    grant is `invalid_grant` (Intuit rotates on every grant), surfacing as a
    spurious failed push. Whole-call scope is deliberate: pilot throughput
    never needs refresh-only granularity, and it keeps the 401/429 retry
    loops trivially race-free.
    """

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        realm_id: str,
        token_store: TokenStore,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._realm_id = realm_id
        self._tokens = token_store
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        self._basic_auth = f"Basic {basic}"
        self._access_token: str | None = None
        # Serializes post_journal_entry (see the class docstring): one refresh
        # lineage must never be consumed by two threads concurrently.
        self._lock = threading.Lock()
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "QboClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _refresh(self) -> None:
        """Mint a fresh access token via the refresh grant (lazily, and on 401)."""
        resp = self._http.post(
            _TOKEN_PATH,
            data={"grant_type": "refresh_token", "refresh_token": self._tokens.load()},
            headers={"Authorization": self._basic_auth},
        )
        if resp.status_code != 200:
            raise QboError(resp.status_code, f"token refresh failed: {_error_message(resp)}")
        payload = resp.json()
        self._access_token = payload["access_token"]
        # Intuit rotates the refresh token on every grant; persist the new one
        # or the NEXT refresh fails with invalid_grant. Through the store, so
        # the lineage outlives this process (D-OH17.7) — `post_journal_entry`
        # already holds the instance lock around this, and DbTokenStore takes
        # a row lock, which is what serializes ACROSS processes.
        self._tokens.store(payload["refresh_token"])

    def post_journal_entry(self, je: dict[str, Any], request_id: str) -> str:
        """POST one journal entry; returns the QBO JournalEntry Id.

        `request_id` is Intuit's idempotency key (max 50 chars): retrying the
        same id replays the original response server-side, never double-posting.

        Holds the client's lock for the whole call — concurrent callers are
        serialized so the lazy refresh (and the 401-triggered one) never race
        on the single rotating refresh token. `_refresh` itself takes no lock,
        so there is no re-entry.
        """
        with self._lock:
            throttled = 0
            refreshed_after_401 = False
            while True:
                if self._access_token is None:
                    self._refresh()
                resp = self._http.post(
                    f"/v3/company/{self._realm_id}/journalentry",
                    params={"requestid": request_id},
                    json=je,
                    headers={"Authorization": f"Bearer {self._access_token}"},
                )
                if resp.status_code == 200:
                    return str(resp.json()["JournalEntry"]["Id"])
                if resp.status_code == 401 and not refreshed_after_401:
                    # Access token expired mid-flight: refresh once and retry.
                    refreshed_after_401 = True
                    self._access_token = None
                    continue
                if resp.status_code == 429:
                    throttled += 1
                    if throttled < _MAX_ATTEMPTS:
                        _sleep(_retry_after_seconds(resp))
                        continue
                raise QboError(resp.status_code, _error_message(resp))
