"""Pay-run API (Pillar C2, Task 8).

Payroll-Admin-gated endpoints over the pay-run service: execute a run for a
property + period, list runs, read a run's PERIOD-grain department aggregates,
and fetch provider results. The provider is resolved per request from the
`create_app` seam (`app.state.get_payroll_provider`) — built from the ACTIVE
ORG's `org_integration_credential` row since OH-17, injected in tests. A
tenant with no row is refused with a named 503, never served an adapter built
from process-wide env.

SECURITY: responses carry NO PII and NO per-employee money, with ONE deliberate
exception — C3's `GET /runs/{id}/lines` returns per-employee gross-to-net to
Payroll Admins only, AUDITED on every read. Every other route returns department
AGGREGATES only. Preflight blocker messages name employees and missing FIELDS,
never values — pinned by the API tests.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import ORG_ADMIN, PAYROLL_ADMIN, Principal, request_session_factory, require_grants
from usali.config import get_settings
from usali.integrations import (
    PAYROLL,
    CredentialUnreadable,
    ResolvedPayroll,
    not_connected_detail,
)
from usali.models import (
    AuditEvent,
    Department,
    Employee,
    PayRun,
    PayRunLine,
    UsaliActualLaborFact,
)
from usali.opener import Opener
from usali.payroll_run import (
    PayRunBlocked,
    PayRunConflict,
    SettlementNotFound,
    SettlementRefused,
    execute_pay_run,
    fetch_pay_run_results,
    settle_worked_hours,
)

router = APIRouter(prefix="/api/payroll")

require_payroll_admin = require_grants(PAYROLL_ADMIN)
# Settlement is a MONEY act (recording that wages were paid off-system) —
# the money roles, not the timecard roles: a GM can reopen a card but
# cannot settle a wage delta.
require_settlement_admin = require_grants(ORG_ADMIN, PAYROLL_ADMIN)

_CENTS = Decimal("0.01")


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


def _provider(request: Request) -> ResolvedPayroll:
    """This tenant's payroll adapter AND its provider name, from the tenant's
    own credential row (OH-17).

    Refused loudly and by name when the row is absent (ADR-010) — never a
    fallback to `settings.payroll_provider`, whose adapter would be built from
    process-wide credentials that are not this tenant's connection.

    Returns the PAIR, never a bare adapter: callers persist `provider_name` as
    the `ProviderEmployeeRef` key, so an adapter separated from its name is a
    mis-pay waiting to happen. See `integrations.ResolvedPayroll`.

    Called from the handler BODY rather than through a `Depends`, so it lands
    after DEPENDENCY-level refusals (the 401 and the payroll_admin 403) rather
    than before them. It does NOT automatically follow a route's own in-body
    refusals — each caller places it deliberately, and they differ:
    `fetch_results` resolves AFTER its 404/409 (a request about a run that does
    not exist must not be answered with the tenant's integration state), while
    `create_run` must resolve BEFORE its duplicate-period 409 because
    `execute_pay_run` raises that conflict and needs the adapter to get there.
    Do not "make these consistent" by hoisting `fetch_results`' call."""
    try:
        resolved: ResolvedPayroll | None = request.app.state.get_payroll_provider(
            request_session_factory(request)
        )
    except CredentialUnreadable as exc:
        # ADR-005: connected, but the stored credential cannot be decrypted
        # (a rotated `field_encryption_key`). Its own refusal, never the
        # "connect Gusto or ADP" one below — that wording would have an
        # operator re-enter credentials that were never the problem, and the
        # next period would fail identically. `str(exc)` names the
        # integration and the likely cause and carries no credential.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(
            status_code=503, detail=not_connected_detail(PAYROLL)
        )
    return resolved


class PayRunCreateBody(BaseModel):
    property: str
    in_period: date


class PayRunSummary(BaseModel):
    pay_run_id: int
    property_id: str
    period_start: date
    period_end: date
    check_date: date
    status: str
    provider: str


class DepartmentAggregate(BaseModel):
    department: str
    hours: Decimal
    gross: Decimal
    employer_burden: Decimal


class PayRunDetail(PayRunSummary):
    failure_reason: str | None
    department_aggregates: list[DepartmentAggregate]


class FetchResultsModel(BaseModel):
    status: str
    lines: int


def _summary(run: PayRun) -> PayRunSummary:
    return PayRunSummary(
        pay_run_id=run.pay_run_id, property_id=run.property_id,
        period_start=run.period_start, period_end=run.period_end,
        check_date=run.check_date, status=run.status, provider=run.provider,
    )


def _department_aggregates(session: Session, run: PayRun) -> list[DepartmentAggregate]:
    """The run's UsaliActualLaborFact rows as named department aggregates
    (name resolution mirrors B3's _labor_sections). NO per-employee money."""
    facts = session.execute(
        select(UsaliActualLaborFact).where(
            UsaliActualLaborFact.pay_run_id == run.pay_run_id
        )
    ).scalars().all()
    aggregates = []
    for f in facts:
        if f.department_id is None:
            name = "Unassigned"
        else:
            dept = session.get(Department, f.department_id)
            name = dept.name if dept is not None else f"department {f.department_id}"
        aggregates.append(DepartmentAggregate(
            department=name,
            hours=Decimal(str(f.hours)).quantize(_CENTS),
            gross=Decimal(str(f.gross)).quantize(_CENTS),
            employer_burden=Decimal(str(f.employer_burden)).quantize(_CENTS),
        ))
    aggregates.sort(key=lambda a: a.department)
    return aggregates


@router.post("/runs", status_code=201)
def create_run(
    body: PayRunCreateBody,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> PayRunSummary:
    """Execute a pay run: preflight -> sync (vault opens HERE, transiently) ->
    submit. Preflight blockers are a 422 whose detail names every blocker;
    a duplicate period is a 409; a provider-failed submit is a 502 (the failed
    run row IS persisted so a re-POST replaces it)."""
    # `settings` here is the pay-period ANCHOR only — genuine deployment
    # config (which Monday the biweekly grid starts on), the same for every
    # tenant. The provider NAME is not config: it comes from the row the
    # adapter itself was built from, so the run and its ProviderEmployeeRefs
    # can never be keyed to a provider that did not run them.
    settings = get_settings()
    resolved = _provider(request)
    opener: Opener = request.app.state.opener
    with _session(request) as session:
        try:
            run = execute_pay_run(
                session, body.property, body.in_period,
                anchor=settings.payroll_period_anchor,
                provider=resolved.adapter,
                provider_name=resolved.provider_name, opener=opener,
                actor=principal.subject,
            )
        except PayRunConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PayRunBlocked as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        session.commit()
        if run.status == "failed":
            raise HTTPException(status_code=502, detail={
                "pay_run_id": run.pay_run_id, "status": "failed",
                "failure_reason": run.failure_reason,
            })
        return _summary(run)


@router.get("/runs")
def list_runs(
    request: Request,
    property_id: str = Query(alias="property"),
    principal: Principal = Depends(require_payroll_admin),
) -> list[PayRunSummary]:
    with _session(request) as session:
        runs = session.execute(
            select(PayRun).where(PayRun.property_id == property_id)
            .order_by(PayRun.period_start)
        ).scalars().all()
        return [_summary(run) for run in runs]


@router.get("/runs/{pay_run_id}")
def get_run(
    pay_run_id: int,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> PayRunDetail:
    with _session(request) as session:
        run = session.get(PayRun, pay_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="pay run not found")
        return PayRunDetail(
            **_summary(run).model_dump(),
            failure_reason=run.failure_reason,
            department_aggregates=_department_aggregates(session, run),
        )


@router.post("/runs/{pay_run_id}/fetch-results")
def fetch_results(
    pay_run_id: int,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> FetchResultsModel:
    """Pull the provider's result into encrypted per-employee lines + department
    aggregates. Idempotent; polling semantics (a still-processing run returns
    its current status with lines=0)."""
    with _session(request) as session:
        run = session.get(PayRun, pay_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="pay run not found")
        if run.status in ("draft", "failed"):
            raise HTTPException(
                status_code=409,
                detail=f"pay run {pay_run_id} is {run.status}; nothing to fetch",
            )
        # Resolved HERE, after the 404/409 — deliberately. A poll for a run
        # that does not exist, or one with nothing to fetch, is answerable
        # without knowing anything about the tenant's integrations, and
        # answering it with a 503 about payroll connectivity would be a worse
        # reply to a worse question. Pinned by
        # `test_an_unknown_run_404s_before_the_connectivity_check`.
        resolved = _provider(request)
        if resolved.provider_name != run.provider:
            # The run was submitted to a DIFFERENT provider than the one this
            # tenant is connected to now — they reconnected payroll between
            # submit and fetch. `provider_run_id` belongs to the OLD provider's
            # namespace and the ref map below is keyed on `run.provider`, so
            # asking the new provider for it is at best a confusing
            # ProviderError and at worst a lookup against a colliding id.
            #
            # This is exactly what `ResolvedPayroll` carries a name FOR, and
            # this call site dropped it until 2026-08-31 — see `_provider`.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"pay run {pay_run_id} was submitted to {run.provider}, but "
                    f"this tenant is now connected to {resolved.provider_name}; "
                    f"reconnect {run.provider} to fetch its results"
                ),
            )
        lines = fetch_pay_run_results(session, run, provider=resolved.adapter)
        session.add(AuditEvent(
            actor_subject=principal.subject, action="fetch_pay_run_results",
            resource_type="pay_run", resource_id=str(run.pay_run_id),
        ))
        session.flush()
        status = run.status
        session.commit()
        return FetchResultsModel(status=status, lines=lines)


class SettlementBody(BaseModel):
    employee_id: int
    # Bounded operator free text (the TimecardAdjustment.reason posture):
    # where/how the delta was paid. Audit-surface only — never echoed
    # into preflight strings. Deliberately NO hours field: the server
    # computes the delta; a caller-typed figure is dead on arrival.
    note: str = Field(min_length=1, max_length=300)


class SettlementModel(BaseModel):
    settlement_id: int
    pay_run_id: int
    employee_id: int
    hours: Decimal


@router.post("/runs/{pay_run_id}/settlements", status_code=201)
def settle_run_worked_hours(
    pay_run_id: int,
    body: SettlementBody,
    request: Request,
    principal: Principal = Depends(require_settlement_admin),
) -> SettlementModel:
    """Record that the CURRENT worked-hours delta on one paid line was paid
    OUTSIDE the integration — the terminal resolution for the worked-hours
    blocker. The server computes the delta (derived minus stored minus
    already-settled) and records exactly that; zero refuses (409). Audited
    BOTH ways: a recorded settlement's audit row points at the settlement
    (which carries the delta, the actor, and the note), and a REFUSED
    attempt writes its own audit row pointing at the probed run — the
    404/409 texture answers per-employee questions (paid on this run? a
    drift outstanding?), and that read must not be free of trail (the I6
    disclosure lens)."""

    def refused(session: Session) -> None:
        session.add(AuditEvent(
            actor_subject=principal.subject,
            action="settle_worked_hours_refused",
            resource_type="pay_run", resource_id=str(pay_run_id),
        ))
        session.commit()

    with _session(request) as session:
        run = session.get(PayRun, pay_run_id)
        if run is None:
            refused(session)
            raise HTTPException(status_code=404, detail="pay run not found")
        try:
            settlement = settle_worked_hours(
                session, run, body.employee_id,
                actor=principal.subject, note=body.note,
            )
        except SettlementNotFound as exc:
            refused(session)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SettlementRefused as exc:
            refused(session)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.add(AuditEvent(
            actor_subject=principal.subject, action="settle_worked_hours",
            resource_type="wage_settlement",
            resource_id=str(settlement.settlement_id),
        ))
        model = SettlementModel(
            settlement_id=settlement.settlement_id,
            pay_run_id=settlement.pay_run_id,
            employee_id=settlement.employee_id,
            hours=Decimal(str(settlement.hours)),
        )
        session.commit()
        return model


class PayRunLineModel(BaseModel):
    employee_id: int
    employee_name: str
    hours: str
    gross: str
    employee_taxes: str
    employer_taxes: str
    net: str


class PayRunLinesModel(BaseModel):
    pay_run_id: int
    status: str
    lines: list[PayRunLineModel]


@router.get("/runs/{pay_run_id}/lines")
def get_run_lines(
    pay_run_id: int,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> PayRunLinesModel:
    """Per-employee gross-to-net for one pay run — the ONLY per-employee money
    read in the system. Payroll-Admin-only and AUDITED on every read (the
    compensation-gate convention): individual pay is PII-adjacent even though
    the aggregates are not. Money fields are the decrypted decimal strings as
    loaded (`EncryptedString` decrypts on load) — a submitted-not-yet-fetched
    run returns the "0" placeholders; the run `status` tells that story."""
    with _session(request) as session:
        run = session.get(PayRun, pay_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="pay run not found")
        rows = session.execute(
            select(PayRunLine, Employee.full_name)
            .join(Employee, PayRunLine.employee_id == Employee.employee_id)
            .where(PayRunLine.pay_run_id == pay_run_id)
            .order_by(Employee.full_name)
        ).all()
        session.add(AuditEvent(
            actor_subject=principal.subject, action="read_pay_run_lines",
            resource_type="pay_run", resource_id=str(pay_run_id),
        ))
        model = PayRunLinesModel(
            pay_run_id=pay_run_id, status=run.status,
            lines=[
                PayRunLineModel(
                    employee_id=line.employee_id, employee_name=name,
                    hours=str(line.hours), gross=line.gross,
                    employee_taxes=line.employee_taxes,
                    employer_taxes=line.employer_taxes, net=line.net,
                )
                for line, name in rows
            ],
        )
        session.commit()
        return model
