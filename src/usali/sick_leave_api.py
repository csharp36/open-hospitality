"""Sick-leave API (E4): balance reads, usage, voids, adjustments.

Payroll-admin-gated, audited both directions, everything DERIVED through
`sick_leave.balance_on`'s fold. Post-review hardening, each line a reproduced
finding:

- Overdraw is judged over the WHOLE timeline (`would_overdraw`) — a
  backdated usage that passed the old point-in-time check drove later dates
  negative.
- Usage requires a primary placement on the day taken: without one the
  hours could never be attributed to any property's books, vanishing from
  every report, and the cap check silently skipped.
- Writers serialize per employee (`SELECT ... FOR UPDATE` on the employee
  row) — check-then-insert raced under concurrency.
- An identical retry is a 409, not a double-booking (the dedup index).
- `usage_void` reverses a mistaken usage AND restores the calendar-year cap
  headroom it consumed — a plain adjustment restored only the balance, so
  the documented correction path caused wrongful denial at the cap boundary.
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from usali.assignments import primary_assignment_on
from usali.auth import PAYROLL_ADMIN, Principal, request_session_factory, require_grants
from usali.models import AuditEvent, Employee, Property, SickLeaveLedger
from usali.sick_leave import balance_on, cap_hours_on, day_length, would_overdraw
from usali.sick_leave_rules import SickLeaveRules, sick_leave_rules_for

router = APIRouter(prefix="/api/payroll")

require_payroll_admin = require_grants(PAYROLL_ADMIN)

_HOURS = Decimal("0.01")
_EIGHT = Decimal("8")


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


class SickLeaveStatus(BaseModel):
    employee_id: int
    balance_hours: str
    # The caps as they apply to THIS employee today. Displayed with the
    # statutory floors when there is no day-length data — the FLOORS are
    # always lawful to show; enforcement (below) skips the cap entirely when
    # D is unknown, which is the employee-favorable direction.
    usage_cap_hours: str | None
    accrual_cap_hours: str | None
    # The recorded employer window choice, machine-readable (the reference
    # doc's "pick AND record" requirement), and this year's net use against
    # the cap so an admin can see headroom.
    usage_cap_window: str | None
    used_this_year_hours: str


class UsageBody(BaseModel):
    hours: Decimal
    on: date


class AdjustmentBody(BaseModel):
    hours: Decimal
    on: date
    # Context for the paper trail (e.g. which incumbent statement was read).
    # Stored on the entry; NEVER echoed into audit rows.
    note: str | None = None


def _rules_for_employee(
    session: Session, employee_id: int, on: date
) -> SickLeaveRules | None:
    primary = primary_assignment_on(session, employee_id, on)
    if primary is None:
        return None
    prop = session.get(Property, primary.property_id)
    if prop is None or prop.wage_jurisdiction is None:
        return None
    return sick_leave_rules_for(prop.wage_jurisdiction)


def _used_in_year(session: Session, employee_id: int, year: int) -> Decimal:
    """Net calendar-year use against the cap: usage minus voids. A voided
    usage was never taken, so it must not consume the entitlement."""
    used = session.execute(
        select(func.coalesce(func.sum(SickLeaveLedger.hours), 0)).where(
            SickLeaveLedger.employee_id == employee_id,
            SickLeaveLedger.entry_type.in_(("usage", "usage_void")),
            SickLeaveLedger.effective_on >= date(year, 1, 1),
            SickLeaveLedger.effective_on <= date(year, 12, 31),
        )
    ).scalar_one()
    return -Decimal(str(used))


def _status(session: Session, employee_id: int, on: date) -> SickLeaveStatus:
    rules = _rules_for_employee(session, employee_id, on)
    d = day_length(session, employee_id, on) or _EIGHT  # display: floors
    return SickLeaveStatus(
        employee_id=employee_id,
        balance_hours=str(balance_on(session, employee_id, on)),
        usage_cap_hours=(
            str(rules.usage_cap_hours(day_length=d).quantize(_HOURS))
            if rules is not None else None
        ),
        accrual_cap_hours=(
            str(rules.accrual_cap_hours(day_length=d).quantize(_HOURS))
            if rules is not None else None
        ),
        usage_cap_window=rules.usage_cap_window if rules is not None else None,
        used_this_year_hours=str(
            _used_in_year(session, employee_id, on.year).quantize(_HOURS)
        ),
    )


def _lock_employee(session: Session, employee_id: int) -> Employee:
    """404 or the employee row, locked FOR UPDATE — every ledger writer for
    one employee serializes here, closing the check-then-insert race."""
    employee = session.execute(
        select(Employee).where(Employee.employee_id == employee_id)
        .with_for_update()
    ).scalar_one_or_none()
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return employee


@router.get("/employees/{employee_id}/sick-leave")
def get_sick_leave(
    employee_id: int, request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> SickLeaveStatus:
    """Balance + caps + year-to-date use, all derived; audited read."""
    with _session(request) as session:
        if session.get(Employee, employee_id) is None:
            raise HTTPException(status_code=404, detail="employee not found")
        status = _status(session, employee_id, date.today())
        session.add(AuditEvent(
            actor_subject=principal.subject, action="read_sick_leave_balance",
            resource_type="employee", resource_id=str(employee_id),
        ))
        session.commit()
        return status


@router.post("/employees/{employee_id}/sick-leave/usage")
def record_usage(
    employee_id: int, body: UsageBody, request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> SickLeaveStatus:
    """Record sick hours taken, dated. Refused by NAME when: no placement on
    the day (unattributable hours vanish from every report); it would drive
    ANY day's balance negative; or it would exceed the calendar-year cap."""
    if body.hours <= 0:
        raise HTTPException(
            status_code=422, detail="usage hours must be positive"
        )
    hours = body.hours.quantize(_HOURS)
    with _session(request) as session:
        _lock_employee(session, employee_id)
        if primary_assignment_on(session, employee_id, body.on) is None:
            raise HTTPException(
                status_code=422,
                detail=f"no primary placement on {body.on.isoformat()} -- "
                       "sick hours taken outside employment cannot be "
                       "attributed to any property's books",
            )
        if would_overdraw(session, employee_id, hours, body.on):
            raise HTTPException(
                status_code=422,
                detail=f"usage on {body.on.isoformat()} would drive the "
                       "balance negative on that or a later date -- sick "
                       "leave cannot be overdrawn",
            )
        rules = _rules_for_employee(session, employee_id, body.on)
        if rules is not None:
            d = day_length(session, employee_id, body.on)
            if d is None:
                # No day-length data: the cap cannot be computed lawfully
                # (flooring denied long-shift staff at cutover). Skipping the
                # cap is the employee-favorable direction and always lawful —
                # the cap is the employer's option, not a mandate.
                pass
            else:
                cap = rules.usage_cap_hours(day_length=d)
                if _used_in_year(session, employee_id, body.on.year) + hours > cap:
                    raise HTTPException(
                        status_code=422,
                        detail=f"usage would exceed the calendar-year cap of "
                               f"{cap} hours ({rules.citation})",
                    )
        session.add(SickLeaveLedger(
            employee_id=employee_id, entry_type="usage", hours=-hours,
            effective_on=body.on,
        ))
        session.add(AuditEvent(
            actor_subject=principal.subject, action="record_sick_leave_usage",
            resource_type="employee", resource_id=str(employee_id),
        ))
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(
                status_code=409,
                detail="an identical usage entry already exists for this "
                       "employee and date -- a genuine second absence of the "
                       "same length should be recorded as an adjustment",
            ) from exc
        status = _status(session, employee_id, body.on)
        session.commit()
        return status


@router.post("/employees/{employee_id}/sick-leave/usage-voids")
def void_usage(
    employee_id: int, body: UsageBody, request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> SickLeaveStatus:
    """Reverse mistakenly recorded usage: restores the balance AND the
    calendar-year cap headroom. Refused when it would void more than the
    year's recorded net usage — a void of hours never taken would mint cap
    headroom that never existed."""
    if body.hours <= 0:
        raise HTTPException(
            status_code=422, detail="void hours must be positive"
        )
    hours = body.hours.quantize(_HOURS)
    with _session(request) as session:
        _lock_employee(session, employee_id)
        if hours > _used_in_year(session, employee_id, body.on.year):
            raise HTTPException(
                status_code=422,
                detail="void exceeds this year's recorded net usage",
            )
        session.add(SickLeaveLedger(
            employee_id=employee_id, entry_type="usage_void", hours=hours,
            effective_on=body.on,
        ))
        session.add(AuditEvent(
            actor_subject=principal.subject, action="void_sick_leave_usage",
            resource_type="employee", resource_id=str(employee_id),
        ))
        session.flush()
        status = _status(session, employee_id, body.on)
        session.commit()
        return status


@router.post("/employees/{employee_id}/sick-leave/adjustments")
def record_adjustment(
    employee_id: int, body: AdjustmentBody, request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> SickLeaveStatus:
    """The audited human path: corrections of BALANCE and opening balances
    (for a mistaken USAGE, use the void — an adjustment cannot restore cap
    headroom). Nonzero, either sign; the note is stored for the paper trail
    and appears in NO audit row and NO error message. Positive adjustments
    record the cap in force on their date, like accruals."""
    if body.hours == 0:
        raise HTTPException(
            status_code=422, detail="an adjustment must be nonzero"
        )
    with _session(request) as session:
        _lock_employee(session, employee_id)
        hours = body.hours.quantize(_HOURS)
        session.add(SickLeaveLedger(
            employee_id=employee_id, entry_type="adjustment",
            hours=hours, effective_on=body.on, note=body.note,
            cap_hours=(
                cap_hours_on(session, employee_id, body.on)
                if hours > 0 else None
            ),
        ))
        session.add(AuditEvent(
            actor_subject=principal.subject,
            action="record_sick_leave_adjustment",
            resource_type="employee", resource_id=str(employee_id),
        ))
        session.flush()
        status = _status(session, employee_id, body.on)
        session.commit()
        return status
