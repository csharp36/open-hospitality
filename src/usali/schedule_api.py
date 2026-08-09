"""Schedule builder API (Pillar D1): shift templates + week/shift CRUD.

Scheduling is GM/org-admin only and property-confined via `assignment_scope`
(the A2.3 rule, exactly as timecard approval: a co-held global VIEW role must
not widen write authority over a property the caller is not assigned to).
Shift routes confine on the SCHEDULE'S property — never on anything the caller
supplied — so a schedule_id or shift_id belonging to another property is a
403, not a write.

Times are property-local wall clock. Request bodies accept "HH:MM" (Pydantic
`datetime.time` parsing); responses serialize times back as zero-padded
"HH:MM" strings (no seconds) — the frontend mirrors this shape in Task 5.

Warnings flag, never block (they live in the projection endpoint, Task 4).
The only 422s here are true data errors: an off-grid week_start, a
business_date outside the week, overlapping shifts for one employee, a
terminated employee, a cross-property employee/department/template, and an
end-before-start shift that does not cross midnight.
"""

import logging
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from usali.auth import (
    ORG_ADMIN,
    PROPERTY_GM,
    Principal,
    effective_roles,
    request_session_factory,
    require_grants,
)
from usali.config import get_settings
from usali.models import (
    EXCLUDE_FROM_PAYROLL,
    AuditEvent,
    Department,
    Employee,
    LaborStandard,
    OccupancyForecast,
    Property,
    Schedule,
    Shift,
    ShiftTemplate,
    Timecard,
    UsaliStatisticFact,
)
from usali.assignments import (
    AmbiguousAssignmentError,
    assignment_at,
    assignments_on,
    employee_ids_serving_property,
    employee_serves_property,
    is_exempt_on,
    property_ids_on,
)
from usali.overtime_rules import rules_for
from usali.rates import HourlyRates, RateError, hourly_rates_on
from usali.schedule_projection import (
    ScheduledShift,
    project_week,
    shift_hours,
    shifts_overlap,
)
from usali.timecards import compute_timecard, period_for
from usali.workforce import assignment_scope

require_scheduler = require_grants(ORG_ADMIN, PROPERTY_GM)

_LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedule")


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


def _has_property_access(
    session: Session, principal: Principal, property_id: str
) -> bool:
    """Whether the caller may act on this property. See _require_property_access
    for why this uses assignment_scope rather than resolve_scope."""
    if ORG_ADMIN in effective_roles(session, principal.subject):
        return True
    return assignment_scope(principal, session).allows_property(property_id)


def _require_property_access(
    session: Session, principal: Principal, property_id: str
) -> None:
    """403 unless the caller may act on this property.

    Deliberately `assignment_scope`, NOT `resolve_scope` (the timecard_api
    rule): the latter short-circuits to all-properties for global VIEW roles,
    which would let an accountant-plus-GM schedule a property they do not run.
    """
    if ORG_ADMIN in effective_roles(session, principal.subject):
        return
    scope = assignment_scope(principal, session)
    if not scope.allows_property(property_id):
        # The detail names neither the property nor the resource — a refusal
        # must not become a side channel for the thing it just refused.
        raise HTTPException(status_code=403, detail="property out of scope")


def _fmt(t: time) -> str:
    return t.strftime("%H:%M")


class TemplateBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    property_id: str = Field(alias="property")
    department_id: int
    name: str = Field(min_length=1, max_length=100)
    start_time: time
    end_time: time
    crosses_midnight: bool = False


class TemplateModel(BaseModel):
    template_id: int
    property_id: str
    department_id: int
    name: str
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    crosses_midnight: bool


class WeekBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    property_id: str = Field(alias="property")
    week_start: date


class ShiftBody(BaseModel):
    business_date: date
    department_id: int
    start_time: time
    end_time: time
    crosses_midnight: bool = False
    employee_id: int | None = None  # None = open shift
    template_id: int | None = None  # provenance only


class ShiftModel(BaseModel):
    shift_id: int
    schedule_id: int
    business_date: str
    department_id: int
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    crosses_midnight: bool
    employee_id: int | None
    template_id: int | None


class WeekModel(BaseModel):
    schedule_id: int
    property_id: str
    week_start: str
    status: str
    version: int
    published_at: str | None  # ISO timestamp of the latest publish; null on draft
    shifts: list[ShiftModel]


def _template_model(tpl: ShiftTemplate) -> TemplateModel:
    return TemplateModel(
        template_id=tpl.template_id, property_id=tpl.property_id,
        department_id=tpl.department_id, name=tpl.name,
        start_time=_fmt(tpl.start_time), end_time=_fmt(tpl.end_time),
        crosses_midnight=tpl.crosses_midnight,
    )


def _shift_model(shift: Shift) -> ShiftModel:
    return ShiftModel(
        shift_id=shift.shift_id, schedule_id=shift.schedule_id,
        business_date=shift.business_date.isoformat(),
        department_id=shift.department_id,
        start_time=_fmt(shift.start_time), end_time=_fmt(shift.end_time),
        crosses_midnight=shift.crosses_midnight,
        employee_id=shift.employee_id, template_id=shift.template_id,
    )


def _week_model(session: Session, sched: Schedule) -> WeekModel:
    shifts = session.execute(
        select(Shift)
        .where(Shift.schedule_id == sched.schedule_id)
        .order_by(Shift.business_date, Shift.start_time, Shift.shift_id)
    ).scalars().all()
    return WeekModel(
        schedule_id=sched.schedule_id, property_id=sched.property_id,
        week_start=sched.week_start.isoformat(), status=sched.status,
        version=sched.version,
        published_at=(
            sched.published_at.isoformat() if sched.published_at is not None else None
        ),
        shifts=[_shift_model(s) for s in shifts],
    )


def _as_scheduled(shift: Shift) -> ScheduledShift:
    return ScheduledShift(
        business_date=shift.business_date, department_id=shift.department_id,
        start_time=shift.start_time, end_time=shift.end_time,
        crosses_midnight=shift.crosses_midnight, employee_id=shift.employee_id,
    )


def _validate_shift(
    session: Session, sched: Schedule, body: ShiftBody,
    *, exclude_shift_id: int | None = None,
) -> None:
    """The hard 422s — TRUE data errors only (warnings are Task 4's job).

    Every referenced row is checked against the SCHEDULE'S property, so a
    cross-property department/employee/template can never enter a week. The
    unknown-id and wrong-property cases share one detail string deliberately:
    a 422 must not be an existence oracle over another property's ids.
    """
    week_end = sched.week_start + timedelta(days=6)
    if not sched.week_start <= body.business_date <= week_end:
        raise HTTPException(
            status_code=422, detail="business_date is outside the schedule week"
        )
    if not body.crosses_midnight and body.end_time <= body.start_time:
        # A zero-or-negative span is not a shift; a genuine overnight shift
        # must say so via crosses_midnight.
        raise HTTPException(
            status_code=422,
            detail="end_time must be after start_time unless the shift crosses midnight",
        )
    dept = session.get(Department, body.department_id)
    if dept is None or dept.property_id != sched.property_id:
        raise HTTPException(
            status_code=422, detail="department does not belong to this property"
        )
    if body.template_id is not None:
        tpl = session.get(ShiftTemplate, body.template_id)
        if tpl is None or tpl.property_id != sched.property_id:
            raise HTTPException(
                status_code=422, detail="template does not belong to this property"
            )
    if body.employee_id is None:
        return  # an open shift references nobody — nothing left to check
    employee = session.get(Employee, body.employee_id)
    if employee is None or not employee_serves_property(
        session, body.employee_id, sched.property_id, body.business_date
    ):
        raise HTTPException(
            status_code=422, detail="employee does not belong to this property"
        )
    if (
        employee.termination_date is not None
        and body.business_date >= employee.termination_date
    ):
        raise HTTPException(
            status_code=422, detail="employee is terminated on this date"
        )
    candidate = ScheduledShift(
        business_date=body.business_date, department_id=body.department_id,
        start_time=body.start_time, end_time=body.end_time,
        crosses_midnight=body.crosses_midnight, employee_id=body.employee_id,
    )
    # The overlap check spans the ADJACENT weeks too: a crosses-midnight
    # Sunday shift in week N spills into week N+1's Monday morning, so the
    # same employee's shifts in this property's week_start ± 7 schedules (when
    # they exist) join the comparison — a cross-boundary double-booking is the
    # same 422 as an in-week one.
    week_starts = [
        sched.week_start - timedelta(days=7),
        sched.week_start,
        sched.week_start + timedelta(days=7),
    ]
    existing = session.execute(
        select(Shift)
        .join(Schedule, Shift.schedule_id == Schedule.schedule_id)
        .where(
            Schedule.property_id == sched.property_id,
            Schedule.week_start.in_(week_starts),
            Shift.employee_id == body.employee_id,
        )
    ).scalars().all()
    for other in existing:
        if other.shift_id == exclude_shift_id:
            continue  # a PUT never overlaps with the row it is replacing
        if shifts_overlap(candidate, _as_scheduled(other)):
            raise HTTPException(
                status_code=422,
                detail="overlaps an existing shift for this employee",
            )


# --- templates -------------------------------------------------------------


@router.post("/templates", status_code=201)
def create_template(
    body: TemplateBody, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> TemplateModel:
    with _session(request) as session:
        # Confinement FIRST: an out-of-scope caller learns nothing about the
        # property, not even whether it exists.
        _require_property_access(session, principal, body.property_id)
        if session.get(Property, body.property_id) is None:
            raise HTTPException(status_code=404, detail="property not found")
        dept = session.get(Department, body.department_id)
        if dept is None or dept.property_id != body.property_id:
            raise HTTPException(
                status_code=422, detail="department does not belong to this property"
            )
        dup = session.execute(
            select(ShiftTemplate).where(
                ShiftTemplate.property_id == body.property_id,
                ShiftTemplate.name == body.name,
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(
                status_code=409, detail="a template with this name already exists"
            )
        tpl = ShiftTemplate(
            property_id=body.property_id, department_id=body.department_id,
            name=body.name, start_time=body.start_time, end_time=body.end_time,
            crosses_midnight=body.crosses_midnight,
        )
        session.add(tpl)
        session.flush()
        model = _template_model(tpl)
        session.commit()
        return model


@router.get("/templates")
def list_templates(
    request: Request,
    property_id: Annotated[str, Query(alias="property")],
    principal: Principal = Depends(require_scheduler),
) -> list[TemplateModel]:
    with _session(request) as session:
        _require_property_access(session, principal, property_id)
        templates = session.execute(
            select(ShiftTemplate)
            .where(ShiftTemplate.property_id == property_id)
            .order_by(ShiftTemplate.name)
        ).scalars().all()
        return [_template_model(t) for t in templates]


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(
    template_id: int, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> Response:
    with _session(request) as session:
        tpl = session.get(ShiftTemplate, template_id)
        if tpl is None:
            raise HTTPException(status_code=404, detail="template not found")
        _require_property_access(session, principal, tpl.property_id)
        referenced = session.execute(
            select(Shift.shift_id).where(Shift.template_id == template_id).limit(1)
        ).first()
        if referenced is not None:
            # Templates are provenance: a shift's template_id records where it
            # came from, so a referenced template cannot vanish out from under
            # its shifts.
            raise HTTPException(
                status_code=409, detail="template is referenced by existing shifts"
            )
        session.delete(tpl)
        session.commit()
    return Response(status_code=204)


# --- weeks -----------------------------------------------------------------


@router.post("/weeks", status_code=201)
def create_week(
    body: WeekBody, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> WeekModel:
    with _session(request) as session:
        _require_property_access(session, principal, body.property_id)
        if session.get(Property, body.property_id) is None:
            raise HTTPException(status_code=404, detail="property not found")
        anchor = get_settings().payroll_period_anchor
        if body.week_start.weekday() != 0 or (body.week_start - anchor).days % 7 != 0:
            # The whole projection promise rests on schedule weeks sharing the
            # payroll workweek's Monday grid — an off-grid week would make
            # weekly-40/7th-day projection silently wrong. The weekday check is
            # belt-and-braces: %7-alignment implies Monday only BECAUSE the
            # anchor is one, and the design's stated rule is "week_start is a
            # Monday", so assert it directly rather than by inheritance.
            raise HTTPException(
                status_code=422,
                detail="week_start is not on the payroll Monday grid",
            )
        existing = session.execute(
            select(Schedule).where(
                Schedule.property_id == body.property_id,
                Schedule.week_start == body.week_start,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=409, detail="schedule week already exists"
            )
        sched = Schedule(property_id=body.property_id, week_start=body.week_start)
        session.add(sched)
        session.flush()
        model = _week_model(session, sched)
        session.commit()
        return model


@router.get("/weeks")
def get_week(
    request: Request,
    property_id: Annotated[str, Query(alias="property")],
    week_start: Annotated[date, Query()],
    principal: Principal = Depends(require_scheduler),
) -> WeekModel:
    with _session(request) as session:
        _require_property_access(session, principal, property_id)
        sched = session.execute(
            select(Schedule).where(
                Schedule.property_id == property_id,
                Schedule.week_start == week_start,
            )
        ).scalar_one_or_none()
        if sched is None:
            raise HTTPException(status_code=404, detail="schedule week not found")
        return _week_model(session, sched)


# --- shifts ----------------------------------------------------------------


def _load_schedule_confined(
    session: Session, principal: Principal, schedule_id: int
) -> Schedule:
    """404 for an unknown week, then confine on the SCHEDULE'S property — the
    caller chose the id; the property it belongs to is not theirs to assert."""
    sched = session.get(Schedule, schedule_id)
    if sched is None:
        raise HTTPException(status_code=404, detail="schedule week not found")
    _require_property_access(session, principal, sched.property_id)
    return sched


@router.post("/weeks/{schedule_id}/shifts", status_code=201)
def add_shift(
    schedule_id: int, body: ShiftBody, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> ShiftModel:
    with _session(request) as session:
        sched = _load_schedule_confined(session, principal, schedule_id)
        _validate_shift(session, sched, body)
        shift = Shift(
            schedule_id=sched.schedule_id, business_date=body.business_date,
            department_id=body.department_id, start_time=body.start_time,
            end_time=body.end_time, crosses_midnight=body.crosses_midnight,
            employee_id=body.employee_id, template_id=body.template_id,
        )
        session.add(shift)
        session.flush()
        model = _shift_model(shift)
        session.commit()
        return model


@router.put("/shifts/{shift_id}")
def update_shift(
    shift_id: int, body: ShiftBody, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> ShiftModel:
    with _session(request) as session:
        shift = session.get(Shift, shift_id)
        if shift is None:
            raise HTTPException(status_code=404, detail="shift not found")
        sched = _load_schedule_confined(session, principal, shift.schedule_id)
        # Full revalidation: a PUT must not smuggle in anything a POST would
        # refuse (a drifted date, a foreign employee, an overlap).
        _validate_shift(session, sched, body, exclude_shift_id=shift_id)
        shift.business_date = body.business_date
        shift.department_id = body.department_id
        shift.start_time = body.start_time
        shift.end_time = body.end_time
        shift.crosses_midnight = body.crosses_midnight
        shift.employee_id = body.employee_id
        shift.template_id = body.template_id
        session.flush()
        model = _shift_model(shift)
        session.commit()
        return model


@router.delete("/shifts/{shift_id}", status_code=204)
def delete_shift(
    shift_id: int, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> Response:
    with _session(request) as session:
        shift = session.get(Shift, shift_id)
        if shift is None:
            raise HTTPException(status_code=404, detail="shift not found")
        _load_schedule_confined(session, principal, shift.schedule_id)
        session.delete(shift)
        session.commit()
    return Response(status_code=204)


# --- projection ------------------------------------------------------------
#
# THE MONEY DISCIPLINE (the B3/C2/C3 rule, verbatim for schedules): the
# projection speaks per-employee in HOURS ONLY and prices at DEPARTMENT
# aggregates. A per-employee cost would hand any scheduler
# rate = cost / hours for the encrypted, Payroll-Admin-gated pay rate — so no
# rate and no per-employee cost value may appear ANYWHERE in the response.
# Departments whose cost comes from fewer than two distinct PRICED employees
# (non-exempt AND carrying a parseable rate) have est_cost suppressed to null:
# counting merely ASSIGNED employees would under-suppress, because a
# department with 2 assigned but 1 priced (the other exempt or rate-less) has
# an aggregate that IS the solo priced employee's cost, and their hours sit in
# the employees table — rate = cost / hours. total_est_cost sums only the
# NON-suppressed departments — complementary suppression, so the total minus
# the shown lines cannot re-derive a hidden one.

_MONEY = Decimal("0.01")
_HOURS = Decimal("0.01")


class ProjectionEmployeeModel(BaseModel):
    """Per-employee HOURS ONLY. Never add a money field here."""

    employee_id: int
    full_name: str
    total_hours: str
    regular_hours: str
    ot_hours: str


class ProjectionWarningModel(BaseModel):
    code: str  # "scheduled_overtime" | "clopening" | "seventh_day"
    employee_id: int
    full_name: str
    business_date: str
    hours: str  # OT hours for scheduled_overtime; "0.00" otherwise — never money


class ProjectionDepartmentModel(BaseModel):
    """Department aggregate. `est_cost` is the PLANNED cost: it is priced
    from the schedule only and is invariant to `as_of` — the plan is what's
    being priced; adherence owns reality. Merged (punched) hours must never
    reprice it: cost varying with `as_of` is a differencing oracle
    (Δcost/Δhours between adjacent `as_of` values = an individual rate)."""

    department: str
    hours: str
    est_cost: str | None  # null = suppressed (< 2 distinct PRICED employees)


class ProjectionModel(BaseModel):
    schedule_id: int
    week_start: str
    # Last day whose punched hours were merged into the projection ("includes
    # actual hours through …"); null when the week is not the current week.
    merged_through: str | None
    employees: list[ProjectionEmployeeModel]
    warnings: list[ProjectionWarningModel]
    departments: list[ProjectionDepartmentModel]
    total_est_cost: str  # sums ONLY non-suppressed departments (complementary)
    suppressed_departments: int
    unpriced_hours: str  # assigned hours carrying no cost (exempt or rate-less)


def _is_exempt(session: Session, employee: Employee, on: date) -> bool:
    """Shared with labor.py and payroll_run.py via usali.assignments so the
    PRICED population is identical across the estimate, the schedule projection
    and the pay run. Three definitions of 'exempt' would give three different
    priced populations, and suppression counts that population."""
    return is_exempt_on(session, employee.employee_id, on)


@router.get("/weeks/{schedule_id}/projection")
def week_projection(
    schedule_id: int, request: Request,
    as_of: Annotated[date | None, Query()] = None,
    principal: Principal = Depends(require_scheduler),
) -> ProjectionModel:
    """Week projection, merged with punched reality when the week is current.

    When `as_of` (optional; default = server today) falls inside the week,
    days strictly before `as_of` use each assigned employee's PUNCHED hours
    (B2's merged timeline — manager corrections honored) in the OT math, so
    projected OT reflects what actually happened; `merged_through` labels the
    last merged day. THE ZERO-PUNCH RULE: a merged day replaces scheduled
    hours ONLY where the employee has worked minutes > 0 that day — an
    elapsed day with no punches keeps its SCHEDULED hours. Mid-week, absent
    punches mean "no data yet" (kiosk down, card not assembled), not "worked
    zero"; zeroing every elapsed day would crater the projection for anyone
    whose punches lag. The adherence endpoint owns no-show truth — the merge
    here is about OT realism only.

    Adjacent weeks' shifts (week_start ± 7d, any status) always join as
    clopening CONTEXT: they participate in rest-pairing but contribute
    nothing to hours, OT, or cost.

    Cost is priced from the SCHEDULE, never merged hours — merged pricing
    would create an as_of differencing oracle (Δcost/Δhours = an individual
    rate): two requests at adjacent `as_of` values isolate one employee's
    single day, and no static suppression floor can defend a marginal query.
    A second, schedule-only engine pass feeds ALL cost aggregation; the
    merged pass drives only hours, warnings, and merged_through.
    """
    with _session(request) as session:
        sched = _load_schedule_confined(session, principal, schedule_id)
        settings = get_settings()
        shifts = session.execute(
            select(Shift).where(Shift.schedule_id == sched.schedule_id)
        ).scalars().all()
        scheduled = [_as_scheduled(s) for s in shifts]

        assigned_ids = {s.employee_id for s in shifts if s.employee_id is not None}
        emp_by_id: dict[int, Employee] = {}
        if assigned_ids:
            emp_by_id = {
                e.employee_id: e
                for e in session.execute(
                    select(Employee).where(Employee.employee_id.in_(assigned_ids))
                ).scalars()
            }

        # Exempt staff are never hourly-costed (the hardened B3 rule: a salary
        # is not a wage), so their rates are never read at all.
        exempt_ids = {
            emp.employee_id
            for emp in emp_by_id.values()
            if _is_exempt(session, emp, sched.week_start)
        }
        # E3: excluded staff are never priced either. Rates live on the
        # placement and survive a reclassification, so "has a rate" is NOT
        # "is payable" — pricing an excluded owner here inflated planned cost
        # with wage that will never be paid AND supplied the second priced
        # body that lifted a one-real-worker department over the >= 2 floor,
        # letting anyone who knows the owner's figure subtract it and read
        # the worker's exact rate. The predicate reached labor.py and
        # payroll_run.py in E3 and missed this gate — the fix-one-gate
        # pattern behind six prior Criticals.
        excluded_ids = {
            emp.employee_id
            for emp in emp_by_id.values()
            if emp.pay_type == EXCLUDE_FROM_PAYROLL
        }

        # Rates resolve PER DATE, not once for the week. Since E2 a rate belongs
        # to a placement and a date range, and a raise landing mid-week is
        # ordinary — pricing the whole week at one sample would pay every day at
        # whichever side of the raise the sample happened to fall.
        #
        # They decrypt on load and NEVER leave this handler: they fold into
        # department aggregates below and are not logged, not returned, and not
        # placed in any error detail.
        rate_cache: dict[tuple[int, date], HourlyRates | None] = {}

        def _rates_for(employee_id: int, on: date) -> HourlyRates | None:
            if employee_id in exempt_ids or employee_id in excluded_ids:
                return None
            key = (employee_id, on)
            if key not in rate_cache:
                try:
                    # `assignment_at` is INSIDE the try: it raises
                    # AmbiguousAssignmentError, which used to escape as an
                    # unhandled 500 that took the whole week's builder down for
                    # one employee's bad placement data.
                    assignment = assignment_at(
                        session, employee_id, sched.property_id, on
                    )
                    rate_cache[key] = (
                        None if assignment is None
                        else hourly_rates_on(session, assignment.assignment_id, on)
                    )
                except (RateError, AmbiguousAssignmentError) as exc:
                    # The projection is a PLANNING view, not a payment. A rate
                    # data error (ambiguous, below-floor, unverifiable, corrupt,
                    # or an ambiguous placement) degrades THIS employee-day to
                    # unpriced -- hours still show, cost is omitted, the
                    # department may suppress -- rather than 500ing the GM's
                    # whole week. The real refusal fires where money is
                    # committed (payroll_run preflight), which names the
                    # employee. The specifics are logged server-side; nothing
                    # about a rate reaches the browser. `exc` carries no figure
                    # (see rates.py), but it is logged as-is, never returned.
                    _LOG.warning(
                        "projection: rate unresolved for employee_id=%s on %s "
                        "(%s: %s); treating as unpriced",
                        employee_id, on.isoformat(), type(exc).__name__, exc,
                    )
                    rate_cache[key] = None
            return rate_cache[key]

        # Adjacent-week clopening context: whatever schedule rows exist for
        # week_start ± 7d (draft or published — the latest saved state is the
        # honest input). Context shifts join rest-pairing only; a boundary
        # warning attributes to the IN-WEEK shift of the pair.
        adjacent_shifts = session.execute(
            select(Shift)
            .join(Schedule, Shift.schedule_id == Schedule.schedule_id)
            .where(
                Schedule.property_id == sched.property_id,
                Schedule.week_start.in_([
                    sched.week_start - timedelta(days=7),
                    sched.week_start + timedelta(days=7),
                ]),
            )
        ).scalars().all()
        context = [_as_scheduled(s) for s in adjacent_shifts]

        # Current-week merge: days strictly before `as_of` take punched hours
        # from B2's merged timeline. The week sits on the payroll Monday grid,
        # so ONE biweekly period contains it whole — period_for(week_start) is
        # the card for every elapsed day.
        week_end = sched.week_start + timedelta(days=6)
        effective_as_of = as_of if as_of is not None else datetime.now(UTC).date()
        merged_through: date | None = None
        actual_hours: dict[int, dict[date, Decimal]] | None = None
        if sched.week_start < effective_as_of <= week_end:
            merged_through = min(effective_as_of - timedelta(days=1), week_end)
            actual_hours = {}
            period_start, _ = period_for(
                sched.week_start, settings.payroll_period_anchor
            )
            for employee_id in assigned_ids:
                card = session.execute(
                    select(Timecard).where(
                        Timecard.employee_id == employee_id,
                        Timecard.period_start == period_start,
                    )
                ).scalar_one_or_none()
                if card is None:
                    continue  # zero punched — scheduled hours stand (see docstring)
                for day in compute_timecard(session, card):
                    # THE ZERO-PUNCH RULE: merge only days with ANY worked
                    # minutes; an elapsed no-punch day keeps its scheduled
                    # hours (mid-week absence of punches is "no data yet",
                    # not "worked zero" — adherence reports the no-show).
                    if (
                        sched.week_start <= day.business_date < effective_as_of
                        and day.worked_minutes > 0
                    ):
                        actual_hours.setdefault(employee_id, {})[
                            day.business_date
                        ] = (Decimal(day.worked_minutes) / 60).quantize(_HOURS)

        # The schedule's property fixes the jurisdiction for this projection.
        sched_property = session.get(Property, sched.property_id)
        if sched_property is None:
            raise HTTPException(status_code=404, detail="unknown property")
        ot_rules = rules_for(sched_property.wage_jurisdiction)

        result = project_week(
            scheduled,
            week_start=sched.week_start,
            exempt_employee_ids=exempt_ids,
            rules=ot_rules,
            meal_threshold_hours=settings.schedule_meal_threshold_hours,
            meal_deduction_minutes=settings.schedule_meal_deduction_minutes,
            min_rest_hours=settings.schedule_min_rest_hours,
            context_shifts=context,
            actual_hours=actual_hours,
        )

        # SCHEDULE-ONLY pass for pricing: cost must be as_of-INVARIANT (see
        # the handler docstring — merged pricing is a differencing oracle).
        # Context shifts contribute nothing to hours or cost, so they are
        # omitted; warnings and the employees table come from the merged
        # `result` above.
        schedule_result = (
            result
            if actual_hours is None
            else project_week(
                scheduled,
                week_start=sched.week_start,
                exempt_employee_ids=exempt_ids,
                rules=ot_rules,
                meal_threshold_hours=settings.schedule_meal_threshold_hours,
                meal_deduction_minutes=settings.schedule_meal_deduction_minutes,
                min_rest_hours=settings.schedule_min_rest_hours,
            )
        )

        # Each assigned employee-day's hours per department. When one day spans
        # departments, the day's priced cost is attributed PROPORTIONALLY by
        # each department's share of that day's hours — never silently dumped
        # on one department.
        day_dept_hours: dict[tuple[int, date], dict[int, Decimal]] = {}
        for s in scheduled:
            if s.employee_id is None:
                continue
            h = shift_hours(
                s,
                meal_threshold_hours=settings.schedule_meal_threshold_hours,
                meal_deduction_minutes=settings.schedule_meal_deduction_minutes,
            )
            by_dept = day_dept_hours.setdefault((s.employee_id, s.business_date), {})
            by_dept[s.department_id] = by_dept.get(s.department_id, Decimal("0")) + h

        # `dept_priced` is the suppression counter: the distinct PRICED
        # employees (non-exempt, parseable rate — exactly the population whose
        # cost accumulates) per department, recorded only for the departments
        # receiving that employee's cost share. Counting ASSIGNED employees
        # instead would under-suppress: 2 assigned / 1 priced means est_cost
        # IS the solo priced employee's cost, hours right there in the table.
        # Open shifts assign nobody and unpriced staff price nothing, so
        # neither ever lifts a department over the >= 2 floor.
        # Cost aggregation consumes ONLY the schedule-only pass — never the
        # merged day_rows. Priced from merged hours, est_cost would vary with
        # `as_of` and become the differencing oracle described above.
        dept_cost: dict[int, Decimal] = {}
        dept_priced: dict[int, set[int]] = {}
        unpriced_hours = Decimal("0")
        for proj in schedule_result.employees:
            for row in proj.day_rows:
                # PER DAY. Before E2 a rate-less employee was skipped whole; now
                # "priced" is a property of an employee-DAY, because a placement
                # can start, end, or be re-rated inside the week.
                rates = _rates_for(proj.employee_id, row.business_date)
                if rates is None:
                    # Exempt or rate-less: hours count everywhere, cost nowhere.
                    unpriced_hours += (
                        row.regular_hours + row.ot_hours + row.dt_hours
                    )
                    continue
                day_cost = (
                    row.regular_hours * rates.regular
                    + row.ot_hours * rates.ot
                    + (
                        Decimal("0") if rates.dot is None
                        else row.dt_hours * rates.dot
                    )
                )
                day_depts = day_dept_hours.get(
                    (proj.employee_id, row.business_date)
                )
                if day_depts is None:
                    continue
                day_total = sum(day_depts.values(), Decimal("0"))
                if day_total <= 0:
                    continue
                for dept_id, dept_h in day_depts.items():
                    dept_cost[dept_id] = (
                        dept_cost.get(dept_id, Decimal("0"))
                        + day_cost * dept_h / day_total
                    )
                    dept_priced.setdefault(dept_id, set()).add(proj.employee_id)

        names: dict[int, str] = {}
        for dept_id in result.department_hours:
            dept = session.get(Department, dept_id)
            names[dept_id] = dept.name if dept is not None else f"department {dept_id}"

        def _emp_name(employee_id: int) -> str:
            # An orphaned reference (shift or punch pointing at a vanished
            # employee row) must degrade, not 500.
            emp = emp_by_id.get(employee_id)
            return emp.full_name if emp is not None else f"employee {employee_id}"

        departments: list[ProjectionDepartmentModel] = []
        total_est_cost = Decimal("0")
        suppressed = 0
        for dept_id in sorted(result.department_hours, key=lambda d: (names[d], d)):
            hours = result.department_hours[dept_id].quantize(_HOURS)
            est_cost: str | None
            if len(dept_priced.get(dept_id, set())) < 2:
                suppressed += 1
                est_cost = None  # complementary: appears in NO total either
            else:
                cost = dept_cost.get(dept_id, Decimal("0")).quantize(
                    _MONEY, rounding=ROUND_HALF_UP
                )
                total_est_cost += cost
                est_cost = str(cost)
            departments.append(ProjectionDepartmentModel(
                department=names[dept_id], hours=str(hours), est_cost=est_cost,
            ))

        return ProjectionModel(
            schedule_id=sched.schedule_id,
            week_start=sched.week_start.isoformat(),
            merged_through=(
                merged_through.isoformat() if merged_through is not None else None
            ),
            employees=[
                ProjectionEmployeeModel(
                    employee_id=p.employee_id,
                    full_name=_emp_name(p.employee_id),
                    total_hours=str(p.total_hours),
                    regular_hours=str(p.regular_hours),
                    ot_hours=str(p.ot_hours),
                )
                for p in result.employees
            ],
            warnings=[
                ProjectionWarningModel(
                    code=w.code, employee_id=w.employee_id,
                    full_name=_emp_name(w.employee_id),
                    business_date=w.business_date.isoformat(), hours=str(w.hours),
                )
                for w in result.warnings
            ],
            departments=departments,
            total_est_cost=str(total_est_cost.quantize(_MONEY)),
            suppressed_departments=suppressed,
            unpriced_hours=str(unpriced_hours.quantize(_HOURS)),
        )


# --- publish ---------------------------------------------------------------


def _publish_lock_stmt(schedule_id: int) -> Select[tuple[Schedule]]:
    """The row lock for the publish path: SELECT ... FOR UPDATE on the
    schedule. Publish is a read-modify-write (version += 1) plus an audit row;
    two racing publishes without the lock would lost-update one version bump
    while still writing two audit rows. `populate_existing` matters: the row
    is already in the identity map from the confined load, and without it the
    locked SELECT would return the PRE-lock attribute values — a waiter that
    acquired the lock after a rival committed would bump a stale version,
    reintroducing the lost update the lock exists to prevent. Kept as a named
    statement so the test suite can assert the FOR UPDATE mechanism directly."""
    return (
        select(Schedule)
        .where(Schedule.schedule_id == schedule_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


@router.post("/weeks/{schedule_id}/publish")
def publish_week(
    schedule_id: int, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> WeekModel:
    """Flip draft -> published, bump the version, stamp and audit.

    Republish, not hard-lock (the D1 deliberate call): editing a published
    week's shifts stays allowed — the week is stale until published again,
    which bumps `version` and writes a second audit row. First publish is
    version 1 (weeks are created at version 0)."""
    with _session(request) as session:
        sched = _load_schedule_confined(session, principal, schedule_id)
        # Confinement passed on the plain load; now take the row lock before
        # the version bump so concurrent publishes serialize (the re-select
        # cannot miss — the id came from the row just loaded).
        sched = session.execute(
            _publish_lock_stmt(sched.schedule_id)
        ).scalar_one()
        sched.status = "published"
        sched.version += 1
        sched.published_by = principal.subject
        sched.published_at = datetime.now(UTC)
        session.add(AuditEvent(
            actor_subject=principal.subject, action="publish_schedule",
            resource_type="schedule", resource_id=str(sched.schedule_id),
        ))
        session.flush()
        model = _week_model(session, sched)
        session.commit()
        return model


# --- labor standards (D2) --------------------------------------------------
#
# A standard converts demand into target HOURS per department per day —
# HOURS-only by design (THE MONEY RULE): a target *cost* would need an average
# department rate, and for a small priced population that average IS an
# individual's rate. D2 adds no new money surface anywhere.

StandardBasis = Literal["fixed_hours_per_day", "minutes_per_occupied_room"]


class StandardBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    property_id: str = Field(alias="property")
    department_id: int
    basis: StandardBasis  # any other basis is a 422 straight from the schema
    value: float = Field(gt=0)  # hours or minutes — never money


class StandardModel(BaseModel):
    standard_id: int
    property_id: str
    department_id: int
    basis: str
    value: str  # "16.00" — the numeric-string convention, 2dp


def _standard_model(std: LaborStandard) -> StandardModel:
    return StandardModel(
        standard_id=std.standard_id, property_id=std.property_id,
        department_id=std.department_id, basis=std.basis,
        value=str(Decimal(str(std.value)).quantize(_HOURS)),
    )


@router.put("/standards")
def upsert_standard(
    body: StandardBody, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> StandardModel:
    """Upsert BY DEPARTMENT (one standard per department — the model's unique
    constraint); re-PUT replaces basis and value."""
    with _session(request) as session:
        _require_property_access(session, principal, body.property_id)
        if session.get(Property, body.property_id) is None:
            raise HTTPException(status_code=404, detail="property not found")
        dept = session.get(Department, body.department_id)
        if dept is None or dept.property_id != body.property_id:
            # Unknown and cross-property share one refusal: no existence
            # oracle over another property's department ids.
            raise HTTPException(
                status_code=422, detail="department does not belong to this property"
            )
        std = session.execute(
            select(LaborStandard).where(
                LaborStandard.department_id == body.department_id
            )
        ).scalar_one_or_none()
        if std is None:
            std = LaborStandard(
                property_id=body.property_id, department_id=body.department_id,
                basis=body.basis, value=body.value,
            )
            session.add(std)
        else:
            std.basis = body.basis
            std.value = body.value
        session.flush()
        model = _standard_model(std)
        session.commit()
        return model


@router.get("/standards")
def list_standards(
    request: Request,
    property_id: Annotated[str, Query(alias="property")],
    principal: Principal = Depends(require_scheduler),
) -> list[StandardModel]:
    with _session(request) as session:
        _require_property_access(session, principal, property_id)
        standards = session.execute(
            select(LaborStandard)
            .where(LaborStandard.property_id == property_id)
            .order_by(LaborStandard.department_id)
        ).scalars().all()
        return [_standard_model(s) for s in standards]


@router.delete("/standards/{standard_id}", status_code=204)
def delete_standard(
    standard_id: int, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> Response:
    with _session(request) as session:
        std = session.get(LaborStandard, standard_id)
        if std is None:
            raise HTTPException(status_code=404, detail="standard not found")
        _require_property_access(session, principal, std.property_id)
        session.delete(std)
        session.commit()
    return Response(status_code=204)


# --- occupancy forecast (D2) -----------------------------------------------
#
# The GM's number is the forecast. The GET decorates each day with history
# HINTS from our own promoted ROOMS_OCCUPIED facts (DAY period, current-year):
# same-day-last-week and a trailing average. Hints inform, never dictate —
# and they are null, never 0, when the facts are absent.


class ForecastDayBody(BaseModel):
    business_date: date
    occupied_rooms: int = Field(ge=0)


class ForecastBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    property_id: str = Field(alias="property")
    days: list[ForecastDayBody]


class ForecastSavedModel(BaseModel):
    property_id: str
    saved: int


class ForecastDayModel(BaseModel):
    business_date: str
    occupied_rooms: int | None  # null = the GM has not forecast this day
    hint_same_day_last_week: int | None
    hint_trailing_avg: int | None


def _hint_int(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rooms_occupied_select(property_id: str) -> Select[tuple[UsaliStatisticFact]]:
    return select(UsaliStatisticFact).where(
        UsaliStatisticFact.property_id == property_id,
        UsaliStatisticFact.metric_code == "ROOMS_OCCUPIED",
        UsaliStatisticFact.period == "DAY",
        UsaliStatisticFact.is_prior_year.is_(False),
    )


@router.put("/forecast")
def upsert_forecast(
    body: ForecastBody, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> ForecastSavedModel:
    """Upsert per (property, business_date). ONE audit row per request — the
    plan-changing write is 'the GM saved a forecast', not each of its days."""
    with _session(request) as session:
        _require_property_access(session, principal, body.property_id)
        if session.get(Property, body.property_id) is None:
            raise HTTPException(status_code=404, detail="property not found")
        for day in body.days:
            row = session.execute(
                select(OccupancyForecast).where(
                    OccupancyForecast.property_id == body.property_id,
                    OccupancyForecast.business_date == day.business_date,
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(OccupancyForecast(
                    property_id=body.property_id, business_date=day.business_date,
                    occupied_rooms=day.occupied_rooms, entered_by=principal.subject,
                ))
            else:
                row.occupied_rooms = day.occupied_rooms
                row.entered_by = principal.subject
            session.flush()  # a duplicated date within one request upserts, not dupes
        session.add(AuditEvent(
            actor_subject=principal.subject, action="write_occupancy_forecast",
            resource_type="property", resource_id=body.property_id,
        ))
        session.commit()
        return ForecastSavedModel(property_id=body.property_id, saved=len(body.days))


@router.get("/forecast")
def get_forecast(
    request: Request,
    property_id: Annotated[str, Query(alias="property")],
    week_start: Annotated[date, Query()],
    principal: Principal = Depends(require_scheduler),
) -> list[ForecastDayModel]:
    with _session(request) as session:
        _require_property_access(session, principal, property_id)
        days = [week_start + timedelta(days=i) for i in range(7)]

        entered: dict[date, int] = {
            f.business_date: f.occupied_rooms
            for f in session.execute(
                select(OccupancyForecast).where(
                    OccupancyForecast.property_id == property_id,
                    OccupancyForecast.business_date.in_(days),
                )
            ).scalars()
        }

        # Same-day-last-week: the fact value at business_date - 7d. Ordered by
        # fact id so a re-promoted date resolves to the LATEST fact.
        same_day: dict[date, int] = {}
        for fact in session.execute(
            _rooms_occupied_select(property_id)
            .where(UsaliStatisticFact.business_date.in_(
                [d - timedelta(days=7) for d in days]
            ))
            .order_by(UsaliStatisticFact.stat_fact_id)
        ).scalars():
            same_day[fact.business_date] = _hint_int(fact.value)

        # Trailing average: the latest <= 7 distinct DAY values strictly before
        # week_start (one value per date — the latest fact for that date),
        # rounded to int. A property with zero facts yields null, never a 500.
        latest_per_day = (
            select(func.max(UsaliStatisticFact.stat_fact_id))
            .where(
                UsaliStatisticFact.property_id == property_id,
                UsaliStatisticFact.metric_code == "ROOMS_OCCUPIED",
                UsaliStatisticFact.period == "DAY",
                UsaliStatisticFact.is_prior_year.is_(False),
                UsaliStatisticFact.business_date < week_start,
            )
            .group_by(UsaliStatisticFact.business_date)
            .order_by(UsaliStatisticFact.business_date.desc())
            .limit(7)
        )
        trailing_values = [
            Decimal(str(f.value))
            for f in session.execute(
                select(UsaliStatisticFact).where(
                    UsaliStatisticFact.stat_fact_id.in_(latest_per_day)
                )
            ).scalars()
        ]
        trailing_avg: int | None = None
        if trailing_values:
            mean = sum(trailing_values, Decimal("0")) / len(trailing_values)
            trailing_avg = int(mean.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

        return [
            ForecastDayModel(
                business_date=d.isoformat(),
                occupied_rooms=entered.get(d),
                hint_same_day_last_week=same_day.get(d - timedelta(days=7)),
                hint_trailing_avg=trailing_avg,
            )
            for d in days
        ]


# --- targets (D2) ----------------------------------------------------------
#
# Target vs scheduled HOURS per department per day — hours-only by design
# (THE MONEY RULE): no rate, no cost, no money-shaped key anywhere in this
# response. A fixed_hours_per_day standard targets its value every day; a
# minutes_per_occupied_room standard multiplies the GM's forecast, and a day
# with NO forecast targets null, NOT zero — absence of a forecast is not zero
# demand, and a silent 0 would make any schedule look over-target. The week
# total sums the non-null days and `days_without_forecast` tells the truth
# about the gap. Scheduled hours come from the SAME meal-adjusted
# `shift_hours` the projection uses (like compares with like), and OPEN
# shifts are included — they are planned coverage.


class TargetDayModel(BaseModel):
    business_date: str
    # null = no standard for the department, or a per-room day with no forecast.
    target_hours: str | None
    scheduled_hours: str


class TargetDepartmentModel(BaseModel):
    department: str
    days: list[TargetDayModel]
    target_total: str | None  # sums the NON-null days; null when every day is
    scheduled_total: str
    # Days whose per-room target is null because the forecast is absent. Zero
    # for fixed standards and for no-standard departments — their nulls are
    # not about the forecast.
    days_without_forecast: int


class TargetsModel(BaseModel):
    schedule_id: int
    week_start: str
    departments: list[TargetDepartmentModel]


@router.get("/weeks/{schedule_id}/targets")
def week_targets(
    schedule_id: int, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> TargetsModel:
    with _session(request) as session:
        sched = _load_schedule_confined(session, principal, schedule_id)
        settings = get_settings()
        days = [sched.week_start + timedelta(days=i) for i in range(7)]

        # Scheduled hours per (department, day) — open shifts included.
        shifts = session.execute(
            select(Shift).where(Shift.schedule_id == sched.schedule_id)
        ).scalars().all()
        scheduled: dict[tuple[int, date], Decimal] = {}
        dept_ids: set[int] = set()
        for s in shifts:
            h = shift_hours(
                _as_scheduled(s),
                meal_threshold_hours=settings.schedule_meal_threshold_hours,
                meal_deduction_minutes=settings.schedule_meal_deduction_minutes,
            )
            key = (s.department_id, s.business_date)
            scheduled[key] = scheduled.get(key, Decimal("0")) + h
            dept_ids.add(s.department_id)

        # Every department with a standard appears too — the GM must see an
        # unstaffed target, not just a target-less staffing.
        standards: dict[int, LaborStandard] = {
            std.department_id: std
            for std in session.execute(
                select(LaborStandard).where(
                    LaborStandard.property_id == sched.property_id
                )
            ).scalars()
        }
        dept_ids |= standards.keys()

        forecast: dict[date, int] = {
            f.business_date: f.occupied_rooms
            for f in session.execute(
                select(OccupancyForecast).where(
                    OccupancyForecast.property_id == sched.property_id,
                    OccupancyForecast.business_date.in_(days),
                )
            ).scalars()
        }

        names: dict[int, str] = {}
        for dept_id in dept_ids:
            dept = session.get(Department, dept_id)
            names[dept_id] = dept.name if dept is not None else f"department {dept_id}"

        departments: list[TargetDepartmentModel] = []
        for dept_id in sorted(dept_ids, key=lambda d: (names[d], d)):
            std = standards.get(dept_id)
            day_models: list[TargetDayModel] = []
            target_total = Decimal("0")
            has_target = False
            scheduled_total = Decimal("0")
            days_without_forecast = 0
            for d in days:
                day_scheduled = scheduled.get((dept_id, d), Decimal("0")).quantize(
                    _HOURS
                )
                scheduled_total += day_scheduled
                target: Decimal | None = None
                if std is not None:
                    if std.basis == "fixed_hours_per_day":
                        target = Decimal(str(std.value)).quantize(_HOURS)
                    elif (rooms := forecast.get(d)) is not None:
                        target = (Decimal(str(std.value)) * rooms / 60).quantize(
                            _HOURS, rounding=ROUND_HALF_UP
                        )
                    else:
                        days_without_forecast += 1  # absence is not zero demand
                if target is not None:
                    target_total += target
                    has_target = True
                day_models.append(TargetDayModel(
                    business_date=d.isoformat(),
                    target_hours=str(target) if target is not None else None,
                    scheduled_hours=str(day_scheduled),
                ))
            departments.append(TargetDepartmentModel(
                department=names[dept_id], days=day_models,
                target_total=(
                    str(target_total.quantize(_HOURS)) if has_target else None
                ),
                scheduled_total=str(scheduled_total.quantize(_HOURS)),
                days_without_forecast=days_without_forecast,
            ))

        return TargetsModel(
            schedule_id=sched.schedule_id,
            week_start=sched.week_start.isoformat(),
            departments=departments,
        )


# --- availability notes (D2) -----------------------------------------------


class AvailabilityNoteBody(BaseModel):
    # <= 300 chars (the column width); anything longer is a 422 from the schema.
    note: str | None = Field(default=None, max_length=300)


class AvailabilityNoteModel(BaseModel):
    employee_id: int
    availability_note: str | None


@router.put("/employees/{employee_id}/availability-note")
def set_availability_note(
    employee_id: int, body: AvailabilityNoteBody, request: Request,
    principal: Principal = Depends(require_scheduler),
) -> AvailabilityNoteModel:
    """A GM-maintained scheduling aid ("can't work Tuesdays") — operational,
    not PII; never money, never medical. Confined on the EMPLOYEE'S property
    (the id the caller picked asserts nothing) and audited like every other
    plan-changing write."""
    with _session(request) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")
        # Scope over ANY property this person works at is enough to record a
        # scheduling note — the note is operational, not money, and a manager
        # who schedules them at either hotel needs to set it.
        placements = property_ids_on(session, employee_id, date.today())
        if not placements:
            raise HTTPException(
                status_code=409, detail="employee has no active assignment"
            )
        if not any(
            _has_property_access(session, principal, p) for p in placements
        ):
            _require_property_access(session, principal, sorted(placements)[0])
        employee.availability_note = body.note
        session.add(AuditEvent(
            actor_subject=principal.subject, action="write_availability_note",
            resource_type="employee", resource_id=str(employee_id),
        ))
        session.commit()
        return AvailabilityNoteModel(
            employee_id=employee_id, availability_note=body.note
        )


# --- adherence (D3) --------------------------------------------------------
#
# Scheduled vs punched per department per day for the ELAPSED part of the
# week (days strictly before `as_of` — a future day has nothing to adhere to,
# and today is mid-shift), plus an exceptions list. Punched hours come from
# B2's MERGED timeline (`compute_timecard`: immutable punches + additive
# audited adjustments), so a manager-corrected day is CLEAN — corrections are
# honored, never re-litigated. HOURS ONLY everywhere (THE MONEY RULE):
# adherence adds no money surface; every employee-level row is hours.


class AdherenceDayModel(BaseModel):
    business_date: str
    scheduled_hours: str
    punched_hours: str


class AdherenceDepartmentModel(BaseModel):
    department: str
    days: list[AdherenceDayModel]
    scheduled_total: str
    punched_total: str


class AdherenceExceptionModel(BaseModel):
    """Per-employee HOURS ONLY. Never add a money field here."""

    code: str  # "no_show" | "unscheduled_punch" | "deviation"
    employee_id: int
    full_name: str
    business_date: str
    scheduled_hours: str
    punched_hours: str


class AdherenceModel(BaseModel):
    schedule_id: int
    week_start: str
    as_of: str
    departments: list[AdherenceDepartmentModel]
    exceptions: list[AdherenceExceptionModel]


@router.get("/weeks/{schedule_id}/adherence")
def week_adherence(
    schedule_id: int, request: Request,
    as_of: Annotated[date | None, Query()] = None,
    principal: Principal = Depends(require_scheduler),
) -> AdherenceModel:
    """Scheduled-vs-punched per department/day with an exceptions list.

    Elapsed days only: `[week_start, min(week_end + 1d, as_of))` — strictly
    before `as_of` (optional; default = server today). A fully-future week
    returns empty days and exceptions.

    Exception rules (per employee per elapsed day, hours aggregated across
    that day's shifts): `no_show` = assigned shift, zero worked minutes;
    `unscheduled_punch` = worked minutes > 0 with no shift that day;
    `deviation` = both > 0 and |punched - scheduled| >=
    `schedule_adherence_deviation_minutes`. Open shifts count toward
    department scheduled hours but can never no-show — nobody was assigned.

    DEPARTMENT ATTRIBUTION: scheduled hours attribute to the SHIFT'S
    department. Punched hours attribute to the day's shift-department for
    scheduled employees — when one day spans departments, proportionally to
    that day's SCHEDULED department split (single-department days, the norm,
    are exact) — and to the employee's HOME department
    (`employee.department_id`, "Unassigned" when null) for unscheduled
    punches.
    """
    with _session(request) as session:
        sched = _load_schedule_confined(session, principal, schedule_id)
        settings = get_settings()
        week_end = sched.week_start + timedelta(days=6)
        effective_as_of = as_of if as_of is not None else datetime.now(UTC).date()
        cutoff = min(week_end + timedelta(days=1), effective_as_of)
        elapsed = [
            sched.week_start + timedelta(days=i)
            for i in range(max((cutoff - sched.week_start).days, 0))
        ]
        as_of_str = effective_as_of.isoformat()
        if not elapsed:
            return AdherenceModel(
                schedule_id=sched.schedule_id,
                week_start=sched.week_start.isoformat(),
                as_of=as_of_str, departments=[], exceptions=[],
            )
        elapsed_set = set(elapsed)

        # Scheduled: per employee/day (exception math) and per employee/day/
        # department (the punched-attribution split). Department keys are
        # `int | None` — None is the "Unassigned" pseudo-department that
        # receives unscheduled punches of home-department-less employees.
        emp_day_sched: dict[tuple[int, date], Decimal] = {}
        emp_day_dept_sched: dict[tuple[int, date], dict[int, Decimal]] = {}
        dept_day_sched: dict[tuple[int | None, date], Decimal] = {}
        shifts = session.execute(
            select(Shift).where(Shift.schedule_id == sched.schedule_id)
        ).scalars().all()
        for s in shifts:
            if s.business_date not in elapsed_set:
                continue
            h = shift_hours(
                _as_scheduled(s),
                meal_threshold_hours=settings.schedule_meal_threshold_hours,
                meal_deduction_minutes=settings.schedule_meal_deduction_minutes,
            )
            dkey: tuple[int | None, date] = (s.department_id, s.business_date)
            dept_day_sched[dkey] = dept_day_sched.get(dkey, Decimal("0")) + h
            if s.employee_id is None:
                continue  # open shift: planned coverage, but nobody can no-show it
            ekey = (s.employee_id, s.business_date)
            emp_day_sched[ekey] = emp_day_sched.get(ekey, Decimal("0")) + h
            split = emp_day_dept_sched.setdefault(ekey, {})
            split[s.department_id] = split.get(s.department_id, Decimal("0")) + h

        # Punched: B2's merged timeline for EVERY property employee holding a
        # timecard in the containing period (the week sits on the payroll
        # Monday grid, so one biweekly card covers it whole) — an employee who
        # was never scheduled this week still surfaces as unscheduled punches.
        period_start, _ = period_for(sched.week_start, settings.payroll_period_anchor)
        cards = session.execute(
            select(Timecard)
            .where(
                Timecard.employee_id.in_(
                    employee_ids_serving_property(
                        session, sched.property_id, sched.week_start
                    )
                ),
                Timecard.period_start == period_start,
            )
        ).scalars().all()
        emp_day_worked: dict[tuple[int, date], int] = {}  # minutes
        for card in cards:
            for day in compute_timecard(session, card):
                if day.business_date in elapsed_set and day.worked_minutes > 0:
                    emp_day_worked[(card.employee_id, day.business_date)] = (
                        day.worked_minutes
                    )

        involved_ids = {k[0] for k in emp_day_sched} | {
            k[0] for k in emp_day_worked
        }
        emp_by_id: dict[int, Employee] = {}
        if involved_ids:
            emp_by_id = {
                e.employee_id: e
                for e in session.execute(
                    select(Employee).where(Employee.employee_id.in_(involved_ids))
                ).scalars()
            }

        # Exceptions per employee per elapsed day.
        threshold = settings.schedule_adherence_deviation_minutes
        exceptions: list[AdherenceExceptionModel] = []
        for emp_id, day_date in set(emp_day_sched) | set(emp_day_worked):
            sched_h = emp_day_sched.get((emp_id, day_date))
            worked_min = emp_day_worked.get((emp_id, day_date), 0)
            code: str | None = None
            if sched_h is not None and worked_min == 0:
                code = "no_show"
            elif sched_h is None and worked_min > 0:
                code = "unscheduled_punch"
            elif sched_h is not None and worked_min > 0 and (
                abs(Decimal(worked_min) - sched_h * 60) >= threshold
            ):
                code = "deviation"
            if code is None:
                continue
            # .get(): an orphaned employee reference degrades, never 500s.
            orphan_safe = emp_by_id.get(emp_id)
            exceptions.append(AdherenceExceptionModel(
                code=code, employee_id=emp_id,
                full_name=(
                    orphan_safe.full_name if orphan_safe is not None
                    else f"employee {emp_id}"
                ),
                business_date=day_date.isoformat(),
                scheduled_hours=str((sched_h or Decimal("0")).quantize(_HOURS)),
                punched_hours=str((Decimal(worked_min) / 60).quantize(_HOURS)),
            ))
        exceptions.sort(key=lambda x: (x.business_date, x.full_name, x.employee_id))

        # Punched attribution to departments (rule in the docstring).
        dept_day_punched: dict[tuple[int | None, date], Decimal] = {}
        for (emp_id, day_date), worked_min in emp_day_worked.items():
            hours = Decimal(worked_min) / 60
            day_split = emp_day_dept_sched.get((emp_id, day_date))
            if day_split:
                day_total = sum(day_split.values(), Decimal("0"))
                for dept_id, dept_h in day_split.items():
                    pkey: tuple[int | None, date] = (dept_id, day_date)
                    dept_day_punched[pkey] = (
                        dept_day_punched.get(pkey, Decimal("0"))
                        + hours * dept_h / day_total
                    )
            else:
                # .get(): an orphaned reference has no home department —
                # attribute to "Unassigned" rather than 500.
                # The department of their assignment AT THIS PROPERTY. A shared
                # employee is Front Office here and Laundry at the other hotel,
                # so an employee-wide department would file the hours wrong.
                home = next(
                    (
                        a.department_id
                        for a in assignments_on(session, emp_id, day_date)
                        if a.property_id == sched.property_id
                    ),
                    None,  # None -> "Unassigned"
                )
                hkey: tuple[int | None, date] = (home, day_date)
                dept_day_punched[hkey] = (
                    dept_day_punched.get(hkey, Decimal("0")) + hours
                )

        dept_ids: set[int | None] = {k[0] for k in dept_day_sched} | {
            k[0] for k in dept_day_punched
        }
        names: dict[int | None, str] = {}
        for dept_key in dept_ids:
            if dept_key is None:
                names[dept_key] = "Unassigned"
                continue
            dept = session.get(Department, dept_key)
            names[dept_key] = (
                dept.name if dept is not None else f"department {dept_key}"
            )

        departments: list[AdherenceDepartmentModel] = []
        for dept_key in sorted(
            dept_ids, key=lambda d: (names[d], -1 if d is None else d)
        ):
            day_models: list[AdherenceDayModel] = []
            scheduled_total = Decimal("0")
            punched_total = Decimal("0")
            for d in elapsed:
                day_sched = dept_day_sched.get((dept_key, d), Decimal("0"))
                day_punched = dept_day_punched.get((dept_key, d), Decimal("0"))
                scheduled_total += day_sched
                punched_total += day_punched
                day_models.append(AdherenceDayModel(
                    business_date=d.isoformat(),
                    scheduled_hours=str(day_sched.quantize(_HOURS)),
                    punched_hours=str(day_punched.quantize(_HOURS)),
                ))
            departments.append(AdherenceDepartmentModel(
                department=names[dept_key], days=day_models,
                scheduled_total=str(scheduled_total.quantize(_HOURS)),
                punched_total=str(punched_total.quantize(_HOURS)),
            ))

        return AdherenceModel(
            schedule_id=sched.schedule_id,
            week_start=sched.week_start.isoformat(),
            as_of=as_of_str, departments=departments, exceptions=exceptions,
        )
