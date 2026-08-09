"""A local ADP-shaped payroll mock (Pillar C2). DEV/TEST ONLY.

Deliberately different from the Gusto mock: camelCase keys inside envelope
objects, money as integer CENTS, and an OAuth client-credentials grant instead
of a static token — so the port is proven against two wire models. Taxes:
18% employee / 11% employer (different from Gusto's, so a symmetric bug in an
adapter cannot accidentally pass both).
"""

import base64
import secrets
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_EMPLOYEE_TAX = Decimal("0.18")
_EMPLOYER_TAX = Decimal("0.11")
_OT = Decimal("1.5")
_DT = Decimal("2")


@dataclass
class _State:
    """Per-app-instance mock state. In-memory only — restart wipes it."""

    tokens: set[str] = field(default_factory=set)
    workers: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_id: int = 1


def _cents(d: Decimal) -> int:
    return int((d * 100).to_integral_value())


def create_mock_adp() -> FastAPI:
    """Build the mock ADP FastAPI app; all state is in-memory per instance."""
    app = FastAPI(title="mock-adp", docs_url=None, redoc_url=None, openapi_url=None)
    state = _State()

    @app.exception_handler(RequestValidationError)
    async def _shape_mismatch(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # G7 (PII lens): FastAPI's default 422 echoes the offending `input`
        # — the full request body, which carries governmentID/bank on the
        # worker endpoints. The mock's posture is "bodies are never
        # echoed", full stop.
        return JSONResponse(status_code=422,
                            content={"detail": "invalid request shape"})

    def check_bearer(request: Request) -> None:
        auth = request.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth.removeprefix("Bearer ") in state.tokens):
            raise HTTPException(status_code=401, detail={"error": "invalid_token"})

    # All handlers are async with no awaits between state reads and writes:
    # requests serialize on the event loop, so the shared state mutations are
    # race-free without locks (same reasoning as the Gusto mock).
    @app.post("/auth/oauth/v2/token")
    async def token(request: Request) -> dict[str, Any]:
        expected = "Basic " + base64.b64encode(b"mock:mock").decode("ascii")
        if request.headers.get("Authorization") != expected:
            raise HTTPException(status_code=401, detail={"error": "invalid_client"})
        form = await request.form()
        if form.get("grant_type") != "client_credentials":
            raise HTTPException(status_code=400, detail={"error": "unsupported_grant_type"})
        tok = secrets.token_urlsafe(16)
        state.tokens.add(tok)
        return {"access_token": tok, "token_type": "Bearer", "expires_in": 3600}

    def _validate_worker(worker: dict[str, Any]) -> None:
        deposits = worker.get("depositAccounts") or []
        # Field NAME only in the detail — never a value (the body carries
        # governmentID/bank PII).
        for f in ("legalName", "governmentID"):
            if not worker.get(f):
                raise HTTPException(status_code=422, detail=f"missing worker.{f}")
        if not deposits:
            raise HTTPException(status_code=422, detail="missing worker.depositAccounts")
        for i, deposit in enumerate(deposits, start=1):
            for f in ("routingNumber", "accountNumber", "allocationType"):
                if not deposit.get(f):
                    raise HTTPException(
                        status_code=422,
                        detail=f"depositAccounts[{i}]: missing {f}",
                    )
        if sum(1 for d in deposits if d.get("allocationType") == "remainder") != 1:
            raise HTTPException(
                status_code=422,
                detail="depositAccounts must contain exactly one remainder",
            )

    @app.post("/hr/v1/workers", status_code=201)
    async def create_worker(body: dict[str, Any], request: Request) -> dict[str, Any]:
        check_bearer(request)
        worker = body.get("worker") or {}
        _validate_worker(worker)
        oid = f"adp-worker-{state.next_id}"
        state.next_id += 1
        state.workers[oid] = {"legalName": worker["legalName"]}
        return {"worker": {"associateOID": oid}}

    @app.put("/hr/v1/workers/{oid}")
    async def update_worker(
        oid: str, body: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        # G4: full-replace update — same validations as create, 404 for a
        # worker never created (the adapter's unknown-id refusal).
        check_bearer(request)
        if oid not in state.workers:
            raise HTTPException(status_code=404, detail="worker not found")
        worker = body.get("worker") or {}
        _validate_worker(worker)
        state.workers[oid] = {"legalName": worker["legalName"]}
        return {"worker": {"associateOID": oid}}

    @app.post("/payroll/v1/payroll-runs", status_code=201)
    async def submit_run(body: dict[str, Any], request: Request) -> dict[str, Any]:
        check_bearer(request)
        run = body.get("payrollRun") or {}
        payments = []
        for e in run.get("earnings", []):
            if e.get("associateOID") not in state.workers:
                raise HTTPException(status_code=422,
                                    detail=f"unknown worker {e.get('associateOID')}")
            # `sickHours` is REQUIRED (G4): a named 422 here is the mock
            # catching a caller that stopped sending the field — exactly
            # the silent-drop the exact-payload tests exist to prevent.
            if "sickHours" not in e:
                raise HTTPException(status_code=422,
                                    detail="missing sickHours")
            rate = Decimal(e["hourlyRateInCents"]) / 100
            gross = (
                Decimal(str(e["regularHours"])) * rate
                + Decimal(str(e["overtimeHours"])) * rate * _OT
                + Decimal(str(e["doubleTimeHours"])) * rate * _DT
                + Decimal(str(e["sickHours"])) * rate
            )
            etax = gross * _EMPLOYEE_TAX
            xtax = gross * _EMPLOYER_TAX
            payments.append({
                "associateOID": e["associateOID"],
                "grossPayInCents": _cents(gross),
                "employeeTaxesInCents": _cents(etax),
                "employerTaxesInCents": _cents(xtax),
                "netPayInCents": _cents(gross) - _cents(etax),
            })
        rid = f"adp-run-{state.next_id}"
        state.next_id += 1
        state.runs[rid] = {"payrollRunID": rid, "processingStatus": "PROCESSED",
                           "workerPayments": payments}
        return {"payrollRun": {"payrollRunID": rid, "processingStatus": "SUBMITTED"}}

    @app.get("/payroll/v1/payroll-runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        check_bearer(request)
        if run_id not in state.runs:
            raise HTTPException(status_code=404, detail=f"unknown payroll run {run_id}")
        return {"payrollRun": state.runs[run_id]}

    return app
