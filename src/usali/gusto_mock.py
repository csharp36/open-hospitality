"""A local Gusto-Embedded-shaped payroll mock (Pillar C2). DEV/TEST ONLY.

Speaks the Gusto public-API *style*: snake_case JSON, money as decimal strings
in dollars, a static bearer token. Processes payrolls synchronously with
deterministic 15% employee / 10% employer taxes so adapter tests are exact.
Accepts only synthetic data; request bodies are never logged.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_TOKEN = "mock"
# The one company this mock's token is scoped to. Only the company READ below
# enforces it: the employee/payroll routes predate it and ignore company_id,
# and narrowing them is a behaviour change for the whole contract suite, not
# part of adding a verification endpoint. Tightening them is a fine follow-on.
_COMPANY_ID = "mock"
_EMPLOYEE_TAX = Decimal("0.15")
_EMPLOYER_TAX = Decimal("0.10")
_OT = Decimal("1.5")
_DT = Decimal("2")
_CENTS = Decimal("0.01")


@dataclass
class _State:
    """Per-app-instance mock state. In-memory only — restart wipes it."""

    employees: dict[str, dict[str, Any]] = field(default_factory=dict)
    payrolls: dict[str, dict[str, Any]] = field(default_factory=dict)
    next_id: int = 1


def _check_bearer(request: Request) -> None:
    if request.headers.get("Authorization") != f"Bearer {_TOKEN}":
        raise HTTPException(status_code=401, detail={"error": "invalid_token"})


def create_mock_gusto() -> FastAPI:
    """Build the mock Gusto FastAPI app; all state is in-memory per instance."""
    app = FastAPI(title="mock-gusto", docs_url=None, redoc_url=None, openapi_url=None)
    state = _State()

    @app.exception_handler(RequestValidationError)
    async def _shape_mismatch(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # G7 (PII lens): FastAPI's default 422 echoes the offending `input`
        # — the full request body, which carries SSN/bank on the employee
        # endpoints. Unreachable via the adapters (they always send dicts),
        # but the mock's posture is "bodies are never echoed", full stop.
        return JSONResponse(status_code=422,
                            content={"detail": "invalid request shape"})

    # All handlers are async with no awaits: requests serialize on the event
    # loop, so the shared state mutations below are race-free without locks.
    # (Sync `def` handlers would run in Starlette's threadpool and race.)
    def _validate_employee(body: dict[str, Any]) -> None:
        for f in ("full_name", "ssn"):
            if not body.get(f):
                # Field NAME only — never the body (it carries SSN/bank PII).
                raise HTTPException(status_code=422, detail=f"missing {f}")
        accounts = body.get("deposit_accounts")
        if not accounts:
            raise HTTPException(status_code=422, detail="missing deposit_accounts")
        for i, acct in enumerate(accounts, start=1):
            for f in ("bank_routing", "bank_account", "allocation_type"):
                if not acct.get(f):
                    # Ordinal + field NAME only — never a value.
                    raise HTTPException(
                        status_code=422,
                        detail=f"deposit account {i}: missing {f}",
                    )
        if sum(1 for a in accounts if a.get("allocation_type") == "remainder") != 1:
            raise HTTPException(
                status_code=422,
                detail="deposit_accounts must contain exactly one remainder",
            )

    @app.get("/v1/companies/{company_id}")
    async def get_company(company_id: str, request: Request) -> dict[str, Any]:
        # OH-17 / D-OH17.8: the read the adapter's `verify()` makes before a
        # credential is stored. It must be able to fail in BOTH the ways a
        # real connection fails — a bad bearer (401, same check as every
        # other route) and a company id this token cannot reach (404) — or
        # the connect endpoint's refusal would be untestable.
        _check_bearer(request)
        if company_id != _COMPANY_ID:
            raise HTTPException(status_code=404, detail="company not found")
        return {"uuid": company_id, "name": "Mock Hotel Co"}

    @app.post("/v1/companies/{company_id}/employees", status_code=201)
    async def create_employee(
        company_id: str, body: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        _check_bearer(request)
        _validate_employee(body)
        uuid = f"gusto-emp-{state.next_id}"
        state.next_id += 1
        state.employees[uuid] = {"full_name": body["full_name"]}
        return {"uuid": uuid, "onboarded": True}

    @app.put("/v1/companies/{company_id}/employees/{uuid}")
    async def update_employee(
        company_id: str, uuid: str, body: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        # G4: full-replace update — same validations as create, 404 for an
        # id this company never created (the adapter's unknown-id refusal).
        _check_bearer(request)
        if uuid not in state.employees:
            raise HTTPException(status_code=404, detail="employee not found")
        _validate_employee(body)
        state.employees[uuid] = {"full_name": body["full_name"]}
        return {"uuid": uuid}

    @app.post("/v1/companies/{company_id}/payrolls", status_code=201)
    async def submit_payroll(
        company_id: str, body: dict[str, Any], request: Request
    ) -> dict[str, Any]:
        _check_bearer(request)
        comps = []
        for e in body.get("entries", []):
            if e.get("employee_uuid") not in state.employees:
                raise HTTPException(status_code=422,
                                    detail=f"unknown employee {e.get('employee_uuid')}")
            # `sick_hours` is REQUIRED (G4): a named 422 here is the mock
            # catching a caller that stopped sending the field — exactly
            # the silent-drop the exact-payload tests exist to prevent.
            if "sick_hours" not in e:
                raise HTTPException(status_code=422,
                                    detail="missing sick_hours")
            rate = Decimal(e["hourly_rate"])
            gross = (
                Decimal(e["regular_hours"]) * rate
                + Decimal(e["overtime_hours"]) * rate * _OT
                + Decimal(e["double_overtime_hours"]) * rate * _DT
                + Decimal(e["sick_hours"]) * rate
            ).quantize(_CENTS)
            etax = (gross * _EMPLOYEE_TAX).quantize(_CENTS)
            xtax = (gross * _EMPLOYER_TAX).quantize(_CENTS)
            comps.append({
                "employee_uuid": e["employee_uuid"],
                "gross_pay": str(gross), "employee_taxes": str(etax),
                "employer_taxes": str(xtax), "net_pay": str(gross - etax),
            })
        uuid = f"gusto-pr-{state.next_id}"
        state.next_id += 1
        state.payrolls[uuid] = {
            "payroll_uuid": uuid, "status": "processed",
            "period_start": body["period_start"], "period_end": body["period_end"],
            "check_date": body["check_date"], "employee_compensations": comps,
        }
        return {"payroll_uuid": uuid, "status": "submitted"}

    @app.get("/v1/companies/{company_id}/payrolls/{payroll_uuid}")
    async def get_payroll(
        company_id: str, payroll_uuid: str, request: Request
    ) -> dict[str, Any]:
        _check_bearer(request)
        if payroll_uuid not in state.payrolls:
            raise HTTPException(status_code=404, detail="payroll not found")
        return state.payrolls[payroll_uuid]

    return app
