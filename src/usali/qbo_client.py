"""HTTP client for QuickBooks Online (P8 Task 4).

A deliberately small, synchronous httpx client that speaks exactly the slice
of Intuit's API the push needs: the OAuth2 refresh grant and journal-entry
create, plus (since OH-17) the one-shot ``authorization_code`` grant that
CONNECTS a tenant — `exchange_authorization_code`, a module function rather
than a client method because it runs before any client exists. Faithful to
the real-Intuit behaviors the mock enforces:

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
import logging
import threading
import time
from typing import Any, Protocol

import httpx

_MAX_ATTEMPTS = 3  # total POST attempts when throttled (429)
_TOKEN_PATH = "/oauth2/v1/tokens/bearer"

# Module-level sleep hook so tests can stub the throttle backoff.
_sleep = time.sleep


logger = logging.getLogger("usali.qbo")


class QboError(Exception):
    """A QBO call failed after the client's retry budget was exhausted."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"QBO {status}: {message}")
        self.status = status
        self.message = message


class QboUnreachable(QboError):
    """We never got an answer from Intuit — DNS, TCP, TLS, or a timeout.

    A subclass, so every existing `except QboError` still covers it (the
    unauthenticated OAuth callback wants exactly that: any refusal becomes a
    400, never a 500). It is a distinct type because ONE caller must tell it
    apart: `qbo_push.push_day` turns a QboError into a per-date `failed`
    ledger row, and an unreachable endpoint is not a per-date outcome — it
    fails identically for every date in the run, so recording N failed rows
    for one network blip is wrong. That caller re-raises this and lets the
    CLI abort the whole push, which is what happened naturally while these
    were bare httpx errors.
    """


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


def _unparseable(resp: httpx.Response) -> str:
    """The safe stand-in for a body we could not read as an Intuit fault.

    NEVER returns the body itself. This string reaches an UNAUTHENTICATED
    caller — `integrations_api.callback` interpolates the QboError into its
    400 — and the body on this path is whatever sits between us and Intuit: a
    proxy error page, a WAF block, a load-balancer 502. Those carry internal
    hostnames, upstream addresses and sometimes a reflected authorization
    code. Echoing 200 bytes of it was a real leak (found in review,
    2026-08-31); the operator-useful half is the STRUCTURED fault below,
    which is Intuit's own text and is safe to surface.

    The body is not lost — it is logged at WARNING, where an operator with
    log access can read it and an anonymous caller cannot."""
    logger.warning(
        "unparseable QBO token-endpoint response: status=%s content_type=%s body=%r",
        resp.status_code, resp.headers.get("content-type"), resp.text[:500],
    )
    return f"unparseable {resp.status_code} response from the QBO token endpoint"


def _json_body(resp: httpx.Response, what: str) -> dict[str, Any]:
    """The response's JSON object, or a QboError.

    A 200 carrying a non-JSON body is not a programming error — it is a
    captive portal, a proxy interstitial, or Intuit serving HTML during an
    incident. Left bare, `resp.json()` raises JSONDecodeError, which on the
    unauthenticated callback route is a 500. Every caller here already knows
    how to turn a QboError into an ordinary refusal."""
    try:
        payload = resp.json()
    except ValueError:
        raise QboError(resp.status_code, f"{what}: {_unparseable(resp)}") from None
    if not isinstance(payload, dict):
        raise QboError(resp.status_code, f"{what}: {_unparseable(resp)}")
    return payload


def _error_message(resp: httpx.Response) -> str:
    """Extract a human-readable message from either Intuit fault shape.

    The return value is safe to put in an HTTP response body — see
    `_unparseable` for why that is a load-bearing property and not a
    stylistic one."""
    try:
        payload = resp.json()
    except ValueError:
        return _unparseable(resp)
    if not isinstance(payload, dict):
        return _unparseable(resp)
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
    return _unparseable(resp)


def exchange_authorization_code(
    code: str,
    *,
    base_url: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Trade Intuit's authorization `code` for the tenant's refresh token
    (OH-17, D-OH17.10/11) — the other half of the consent flow, and the ONLY
    way `accounting` can be connected.

    Lives here rather than in the router so it shares `_TOKEN_PATH` and
    `_error_message` with `QboClient._refresh`: the token endpoint and the
    Intuit fault shapes are one fact about the provider, and a second copy is
    a second thing to get wrong when Intuit moves either.

    Not a `QboClient` method, and not built on one, because there is no client
    yet: a client needs a `TokenStore`, and the token this call returns is
    what the store will hold. The client is constructed later, per request,
    from the credential row this token lands in (`integrations.resolve_qbo`).

    `code` is SINGLE-USE at Intuit. That is load-bearing beyond tidiness: it
    is what makes replaying a whole captured callback useless, and it is the
    reason D-OH17.11 carries no server-side nonce store. Do not add a caching
    or retry layer around it; a retry of a consumed code is a guaranteed
    failure, and a cache of the result would resurrect the replay window that
    property closes.

    It closes replay of a whole callback and nothing wider — a captured
    `state` submitted with a FRESH code still binds this grant to the org
    named in that state. That residual is accepted and documented at
    `integrations_api.sign_state`; it is not something this function can fix.

    `redirect_uri` must be the BYTE-IDENTICAL string sent to the consent
    endpoint — Intuit compares them exactly, and a mismatch fails here, long
    after the URL looked fine. One expression feeds both call sites
    (`integrations_api.qbo_redirect_uri`).

    Raises `QboError` on any non-200, on a body that is not JSON, on a
    network failure reaching Intuit, or on a response carrying no refresh
    token,
    so a spent or bogus code is an ordinary refusal the router turns into a
    400 rather than a 500.
    """
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    with httpx.Client(
        base_url=base_url, transport=transport, headers={"Accept": "application/json"}
    ) as http:
        try:
            resp = http.post(
                _TOKEN_PATH,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Authorization": f"Basic {basic}"},
            )
        except httpx.HTTPError as exc:
            # "Intuit is unreachable" is a ROUTINE operational condition, not a
            # bug: a DNS blip, a timeout, an egress rule. Unhandled it became a
            # 500 on the one route in the app that is unauthenticated. The
            # message names the exception TYPE only — the exception's own text
            # carries the resolved upstream address (found in review,
            # 2026-08-31).
            raise QboUnreachable(
                502, f"could not reach the QBO token endpoint: {type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            raise QboError(
                resp.status_code,
                f"authorization-code grant failed: {_error_message(resp)}",
            )
        payload = _json_body(resp, "authorization-code grant")
    token = payload.get("refresh_token")
    if not isinstance(token, str) or not token:
        # A 200 with no refresh token would otherwise become a KeyError 500,
        # or — worse — an empty string stored as this tenant's credential,
        # which reads as "connected" to the checklist's presence probe.
        raise QboError(200, "authorization-code grant returned no refresh_token")
    return token


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

    Refresh-token rotation is written back through a `TokenStore` port. Every
    grant consumes the token the store handed out and the replacement is
    written back before `_refresh` returns, so the lineage lives in the STORE
    and not in this object: what callers must share is the STORE, not the
    client. Rebuilding a client over the same store — which is exactly what a
    restarted process does — refreshes fine.

    What the port does not do on its own is survive the process. With
    `StaticTokenStore` the lineage dies with it, so a second process
    bootstrapped from the same static token still gets `invalid_grant`; that
    is the standing limitation, and a durable store
    (`usali.integrations.DbTokenStore`) is what removes it.

    Thread safety: `post_journal_entry` holds an internal lock for its whole
    body (lazy refresh + POST + retry loops), so concurrent callers on one
    shared instance are serialized. Without it, two threads could both see a
    missing access token and refresh with the SAME refresh token — the loser's
    grant is `invalid_grant` (Intuit rotates on every grant), surfacing as a
    spurious failed push. Whole-call scope is deliberate: pilot throughput
    never needs refresh-only granularity, and it keeps the 401/429 retry
    loops trivially race-free. That lock is per-INSTANCE, so it protects only
    callers sharing one client — and since OH-17 the portal shares none: it
    builds a client per request from the active org's credential row, because
    one process-wide client is one tenant's connection serving every tenant.
    Two concurrent pushes therefore fork the refresh exactly as two processes
    would. That outcome is accepted and documented in
    `integrations.DbTokenStore`: the loser's grant fails visibly, the winner's
    rotation is durable in the row, and a retry succeeds. Do not reach for a
    shared client to fix it — read that docstring first.
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
        try:
            resp = self._http.post(
                _TOKEN_PATH,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._tokens.load(),
                },
                headers={"Authorization": self._basic_auth},
            )
        except httpx.HTTPError as exc:
            raise QboUnreachable(
                502, f"could not reach the QBO token endpoint: {type(exc).__name__}"
            ) from exc
        if resp.status_code != 200:
            raise QboError(resp.status_code, f"token refresh failed: {_error_message(resp)}")
        payload = _json_body(resp, "token refresh")
        access = payload.get("access_token")
        rotated = payload.get("refresh_token")
        # A 200 missing either field was a KeyError -> 500. Both are refusals:
        # without `access_token` there is nothing to call with, and without
        # `refresh_token` the rotation below would store None and kill the
        # tenant's connection on the next call.
        if not isinstance(access, str) or not access:
            raise QboError(200, "token refresh returned no access_token")
        if not isinstance(rotated, str) or not rotated:
            raise QboError(200, "token refresh returned no refresh_token")
        self._access_token = access
        # Intuit rotates the refresh token on every grant; persist the new one
        # or the NEXT refresh fails with invalid_grant. Through the store, so
        # the token has a durable home and the lineage CAN outlive this
        # process (D-OH17.7). `post_journal_entry` holds the instance lock
        # around this, so threads sharing THIS client are serialized here.
        #
        # Concurrent refreshes are otherwise NOT serialized, and that is a
        # decision (settled 2026-08-30; see `integrations.DbTokenStore`). The
        # lock above is per-INSTANCE, and D-OH17.6 REMOVED the memoizer that
        # made concurrent callers share one client — so this covers less than
        # it looks like it does. The critical section is
        # load() -> grant -> store(), which no lock taken inside either store
        # method can cover, and this method is exactly why one spanning them
        # is unsafe: the `raise` above leaves without ever calling `store()`,
        # so a lock opened by `load()` would have no release path on the
        # routine failure of an expired or revoked token. Two concurrent
        # pushes for one tenant can therefore both spend the same token; the
        # loser gets invalid_grant here and its push fails visibly, while the
        # winner's rotated token is in the row, so a retry succeeds.
        self._tokens.store(rotated)

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
