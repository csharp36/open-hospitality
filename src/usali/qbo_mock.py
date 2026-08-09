"""Mock QuickBooks Online server (P8 Task 3).

A local stand-in for Intuit's OAuth2 + Accounting API, faithful enough in its
request/response shapes that pointing the P8 push client at real QBO later is
only a base-URL + credentials change. Endpoints:

- ``POST /oauth2/v1/tokens/bearer`` — token grant (``authorization_code`` as a
  test bootstrap, ``refresh_token`` with rotation; the settings-default token
  ``"mock"`` is pre-seeded so the out-of-the-box config's first refresh grant
  succeeds). Requires an HTTP Basic ``Authorization`` header like real Intuit
  (credentials are not validated, but the header shape is enforced so the
  client must send it correctly).
- ``POST /v3/company/{realm}/journalentry?requestid=X`` — bearer-auth'd
  journal-entry create with Intuit-style ``requestid`` idempotency: a repeated
  requestid replays the stored response verbatim, never double-posting.
- ``GET /v3/company/{realm}/query?query=select * from Account`` — serves the
  chart of accounts from ``mapping/qbo_accounts.yaml`` in Intuit's
  ``QueryResponse`` shape.
- ``GET /mock/journalentries`` — mock-only introspection: everything posted.

Error shapes mirror Intuit's two fault families: auth failures are 401 with a
lowercase ``{"fault": {"error": [...], "type": "AUTHENTICATION"}}`` body;
business validation failures are 400 with a capitalized
``{"Fault": {"Error": [...], "type": "ValidationFault"}}`` body.

Amounts: real QBO sends JSON numbers, so ``Amount`` is accepted as either a
number or a string — but the debit/credit balance check always converts via
``Decimal(str(amount))``, never comparing binary floats. Line amounts must be
positive: direction is carried by ``PostingType``, never by sign (real QBO
rejects non-positive amounts — this catches sign-flip bugs in the JE builder).

Fidelity guards beyond happy-path shapes: ``/v3/*`` calls must send an
``Accept`` header that admits JSON (real QBO defaults to XML responses
otherwise — the mock returns a loud 400 instead of silently serving JSON);
``requestid`` is capped at Intuit's 50-character limit; every Line must carry
``"DetailType": "JournalEntryLineDetail"``.

Concurrency: handlers are ``async def`` with no awaits, so every request
serializes on the event loop — shared in-memory state (token rotation, JE id
allocation, replay map) needs no locks even under a real uvicorn server.

Fault injection for retry-path tests: send ``X-Mock-Fault: throttle-once:<nonce>``
to get one 429 (with ``Retry-After: 1``) or ``expired-once:<nonce>`` to get one
401, keyed on the full header value — distinct nonces are independent one-shots.

All state (tokens, journal entries, requestid replays, fault nonces) is
in-memory per app instance and lost on restart; e2e runs reseed by restarting.
"""

import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

_DEFAULT_ACCOUNTS = Path(__file__).resolve().parents[2] / "mapping" / "qbo_accounts.yaml"
_ACCOUNT_QUERY = "select * from account"
_FAULT_HEADER = "X-Mock-Fault"
_MAX_REQUESTID_LEN = 50  # Intuit's documented requestid limit
# Intuit refresh tokens live ~101 days; the mock reports the same fixed value.
_REFRESH_TOKEN_TTL_SECONDS = 8726400


@dataclass
class _State:
    """Per-app-instance mock state. In-memory only — restart wipes it."""

    access_tokens: dict[str, float] = field(default_factory=dict)  # token -> monotonic deadline
    refresh_tokens: set[str] = field(default_factory=set)
    journal_entries: list[dict[str, Any]] = field(default_factory=list)
    replay_responses: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    faults_seen: set[str] = field(default_factory=set)
    next_je_id: int = 1


def _load_accounts(accounts_path: Path) -> list[dict[str, str]]:
    """Load the CoA YAML into Intuit Account rows (Id, Name, AcctNum, AccountType)."""
    raw = yaml.safe_load(accounts_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{accounts_path} must be a YAML list of accounts")
    return [
        {
            "Id": str(i),
            "Name": str(entry["name"]),
            "AcctNum": str(entry["account_code"]),
            "AccountType": str(entry["account_type"]),
        }
        for i, entry in enumerate(raw, start=1)
    ]


def _oauth_error(status: int, error: str) -> JSONResponse:
    """OAuth2 token-endpoint error, RFC 6749 shape (what Intuit's OAuth host returns)."""
    return JSONResponse(status_code=status, content={"error": error})


def _auth_fault() -> JSONResponse:
    """401 with Intuit's lowercase AUTHENTICATION fault shape."""
    return JSONResponse(
        status_code=401,
        content={
            "fault": {
                "error": [
                    {
                        "message": "message=AuthenticationFailed; errorCode=003200; "
                        "statusCode=401",
                        "detail": "SystemFault",
                        "code": "3200",
                    }
                ],
                "type": "AUTHENTICATION",
            }
        },
    )


def _validation_fault(message: str, detail: str, code: str = "6000") -> JSONResponse:
    """400 with Intuit's capitalized ValidationFault shape."""
    return JSONResponse(
        status_code=400,
        content={
            "Fault": {
                "Error": [{"Message": message, "Detail": detail, "code": code, "element": ""}],
                "type": "ValidationFault",
            }
        },
    )


def _validate_lines(lines: object, coa_codes: frozenset[str]) -> JSONResponse | None:
    """Validate JE lines against required fields, the CoA, and Σdebits == Σcredits."""
    if not isinstance(lines, list) or not lines:
        return _validation_fault(
            "Required parameter Line is missing", "JournalEntry must have at least one Line"
        )
    totals = {"Debit": Decimal(0), "Credit": Decimal(0)}
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            return _validation_fault("Invalid Line", f"Line[{i}] must be an object")
        if line.get("DetailType") != "JournalEntryLineDetail":
            return _validation_fault(
                "Invalid DetailType",
                f"Line[{i}].DetailType must be 'JournalEntryLineDetail', "
                f"got {line.get('DetailType')!r}",
            )
        detail = line.get("JournalEntryLineDetail")
        if not isinstance(detail, dict):
            return _validation_fault(
                "Required parameter JournalEntryLineDetail is missing",
                f"Line[{i}] has no JournalEntryLineDetail",
            )
        posting_type = detail.get("PostingType")
        if posting_type not in ("Debit", "Credit"):
            return _validation_fault(
                "Invalid PostingType",
                f"Line[{i}].JournalEntryLineDetail.PostingType must be Debit or Credit, "
                f"got {posting_type!r}",
            )
        account_ref = detail.get("AccountRef")
        account = account_ref.get("value") if isinstance(account_ref, dict) else None
        if not isinstance(account, str) or account not in coa_codes:
            return _validation_fault(
                "Invalid account",
                f"Line[{i}] AccountRef.value {account!r} is not in the chart of accounts",
                code="2500",
            )
        amount = line.get("Amount")
        if amount is None or isinstance(amount, bool):
            return _validation_fault(
                "Required parameter Amount is missing", f"Line[{i}] has no Amount"
            )
        try:
            value = Decimal(str(amount))
        except InvalidOperation:
            return _validation_fault(
                "Invalid Amount", f"Line[{i}] Amount {amount!r} is not a number"
            )
        if not value.is_finite() or value <= 0:
            # is_finite first: Decimal("NaN") passes construction but `<= 0`
            # raises InvalidOperation. Direction lives in PostingType, never in
            # the sign; real QBO rejects non-positive line amounts (sign-flip
            # bugs land here).
            return _validation_fault(
                "Invalid Amount", f"Line[{i}] Amount must be positive, got {amount!r}"
            )
        totals[posting_type] += value
    if totals["Debit"] != totals["Credit"]:
        return _validation_fault(
            "Transaction is out of balance",
            f"Total debits ({totals['Debit']}) must equal total credits ({totals['Credit']})",
            code="6970",
        )
    return None


def create_mock_qbo(
    accounts_path: Path = _DEFAULT_ACCOUNTS,
    *,
    token_ttl_seconds: float = 3600.0,
) -> FastAPI:
    """Build the mock QBO FastAPI app; all state is in-memory per instance.

    ``token_ttl_seconds`` controls access-token lifetime against a monotonic
    clock (tests pass 0.0 to mint born-expired tokens instead of sleeping).
    """
    accounts = _load_accounts(accounts_path)
    coa_codes = frozenset(a["AcctNum"] for a in accounts)
    state = _State()
    # Pre-seed the settings-default refresh token ("mock") so the out-of-the-box
    # `usali qbo-mock` + `usali qbo-push` flow works without a consent-flow
    # stand-in: the client's first refresh grant with USALI_QBO_REFRESH_TOKEN's
    # default succeeds. Rotation still applies after first use — the token is
    # consumed and rotated like any other.
    state.refresh_tokens.add("mock")
    app = FastAPI(title="mock-qbo", docs_url=None, redoc_url=None)

    def _mint_tokens() -> dict[str, object]:
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        state.access_tokens[access] = time.monotonic() + token_ttl_seconds
        state.refresh_tokens.add(refresh)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": int(token_ttl_seconds),
            "x_refresh_token_expires_in": _REFRESH_TOKEN_TTL_SECONDS,
            "token_type": "bearer",
        }

    def _check_bearer(request: Request) -> JSONResponse | None:
        """Accept guard, then fault injection, then bearer validation.

        None means authorized. Real QBO v3 responds in XML unless the client
        sends ``Accept: application/json`` — a client that forgets it would
        work against a silently-JSON mock and break against Intuit, so the
        mock refuses loudly instead.
        """
        accept = request.headers.get("Accept", "")
        if "application/json" not in accept and "*/*" not in accept:
            return JSONResponse(
                status_code=400,
                content={
                    "error": "mock requires Accept: application/json — "
                    "real QBO would return XML"
                },
            )
        fault = request.headers.get(_FAULT_HEADER)
        if fault and fault not in state.faults_seen:
            if fault.startswith("throttle-once"):
                state.faults_seen.add(fault)
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": "1"},
                    content={
                        "Fault": {
                            "Error": [
                                {
                                    "Message": "ThrottleExceeded",
                                    "Detail": "Throttle exceeded",
                                    "code": "3001",
                                    "element": "",
                                }
                            ],
                            "type": "ValidationFault",
                        }
                    },
                )
            if fault.startswith("expired-once"):
                state.faults_seen.add(fault)
                return _auth_fault()
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return _auth_fault()
        deadline = state.access_tokens.get(auth.removeprefix("Bearer "))
        if deadline is None or time.monotonic() >= deadline:
            return _auth_fault()
        return None

    # All handlers are async with no awaits: requests serialize on the event
    # loop, so the shared state mutations below are race-free without locks.
    # (Sync `def` handlers would run in Starlette's threadpool and race.)
    @app.post("/oauth2/v1/tokens/bearer")
    async def token(
        request: Request,
        grant_type: Annotated[str, Form()],
        code: Annotated[str | None, Form()] = None,
        refresh_token: Annotated[str | None, Form()] = None,
    ) -> JSONResponse:
        # Real Intuit authenticates client_id:client_secret via HTTP Basic; the
        # mock accepts any credentials but requires the header shape.
        if not request.headers.get("Authorization", "").startswith("Basic "):
            return _oauth_error(401, "invalid_client")
        if grant_type == "authorization_code":
            if not code:
                return _oauth_error(400, "invalid_request")
            return JSONResponse(content=_mint_tokens())
        if grant_type == "refresh_token":
            if refresh_token not in state.refresh_tokens:
                return _oauth_error(400, "invalid_grant")
            state.refresh_tokens.discard(refresh_token)  # Intuit rotates refresh tokens
            return JSONResponse(content=_mint_tokens())
        return _oauth_error(400, "unsupported_grant_type")

    @app.post("/v3/company/{realm}/journalentry")
    async def create_journal_entry(
        realm: str,
        body: dict[str, Any],
        request: Request,
        requestid: str | None = None,
    ) -> JSONResponse:
        if (denied := _check_bearer(request)) is not None:
            return denied
        if requestid is not None and len(requestid) > _MAX_REQUESTID_LEN:
            return _validation_fault(
                "Invalid requestid",
                f"requestid must be at most {_MAX_REQUESTID_LEN} characters, "
                f"got {len(requestid)}",
            )
        if requestid is not None:
            replayed = state.replay_responses.get((realm, requestid))
            if replayed is not None:
                return JSONResponse(content=replayed)
        if (invalid := _validate_lines(body.get("Line"), coa_codes)) is not None:
            return invalid
        entry = {**body, "Id": str(state.next_je_id)}
        state.next_je_id += 1
        state.journal_entries.append(entry)
        response = {"JournalEntry": entry}
        if requestid is not None:
            state.replay_responses[(realm, requestid)] = response
        return JSONResponse(content=response)

    @app.get("/v3/company/{realm}/query")
    async def query(realm: str, query: str, request: Request) -> JSONResponse:
        if (denied := _check_bearer(request)) is not None:
            return denied
        if " ".join(query.split()).lower() != _ACCOUNT_QUERY:
            return _validation_fault(
                "Unsupported query", f"mock QBO only supports {_ACCOUNT_QUERY!r}, got {query!r}"
            )
        return JSONResponse(content={"QueryResponse": {"Account": accounts}})

    @app.get("/mock/journalentries")
    async def list_journal_entries() -> JSONResponse:
        """Mock-only introspection endpoint (not part of the Intuit API)."""
        return JSONResponse(content=state.journal_entries)

    return app
