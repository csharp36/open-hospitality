"""Workforce scope model + hybrid RBAC resolution (Pillar A2.2, org-scoped
grants since L4).

`resolve_scope` is hybrid: it trusts the token's `scopes` claim when present
(fast, stateless) and falls back to the authoritative `role_assignment` DB table
otherwise. Since L4 (Pillar L decision 4) the all-properties short-circuit
comes from the DB too: an ORG-WIDE grant row (`property_id` NULL) read under
the active org's RLS — never from token roles, which are realm-global and
would follow an org_admin of tenant A into tenant B.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from usali.assignments import assignments_on, primary_assignment_on, property_ids_on
from usali.auth import (
    DEPARTMENT_MANAGER,
    OPERATOR_ROLES,
    ORG_ADMIN,
    PAYROLL_ADMIN,
    PROPERTY_GM,
    Principal,
    effective_roles,
    request_session_factory,
    require_grants,
    require_operator,
)
from usali.keycloak_admin import KeycloakAdmin
from usali.models import (
    AssignmentRate,
    AuditEvent,
    Department,
    Employee,
    EmployeeAssignment,
    Property,
    RoleAssignment,
    Timecard,
    UsaliLaborFact,
)
from usali.onboarding import (
    OnboardRequest,
    onboard_employee,
    pay_type_violation,
    terminate_employee,
)
from usali.rates import rate_on

@dataclass(frozen=True)
class Scope:
    all_properties: bool
    properties: frozenset[str]
    departments: frozenset[tuple[str, int]]

    def allows_property(self, property_id: str) -> bool:
        # NOTE: deliberately does NOT fall through to `departments` — a
        # department-scoped assignment (e.g. department_manager) grants access
        # to that one department, not property-wide access. See
        # test_scope_from_db_when_no_claim.
        return self.all_properties or property_id in self.properties

    def allows_department(self, property_id: str, department_id: int) -> bool:
        return (
            self.all_properties
            or property_id in self.properties
            or (property_id, department_id) in self.departments
        )


def _build_scope(entries: Iterable[tuple[str, int | None]]) -> Scope:
    entries = list(entries)
    properties = frozenset(p for p, d in entries if d is None)
    departments = frozenset((p, d) for p, d in entries if d is not None)
    return Scope(all_properties=False, properties=properties, departments=departments)


def _scope_from_claim(
    scopes: Iterable[tuple[str, int | None]], session: Session
) -> Scope:
    """Build a Scope from the token's realm-global `scopes` claim, INTERSECTED
    with the properties visible under the active org's RLS (L8-F5).

    property_id is globally unique, so an unfiltered claim carrying another
    org's property_id would open that org's property gate here — the token
    follows an operator across the tenant wall. `select(Property.property_id)`
    on the bound session returns ONLY the active org's properties (the walls
    filter it), so an entry naming a property outside the active org is
    dropped before it can name a gate. (Double-backstopped today — RLS empties
    the room and prod ships no scopes mapper — but the gate must not be able to
    NAME a foreign property in the first place.)"""
    visible = set(session.execute(select(Property.property_id)).scalars())
    return _build_scope((p, d) for p, d in scopes if p in visible)


def resolve_scope(principal: Principal, session: Session) -> Scope:
    """The caller's VIEW scope in the session's org. The all-properties
    short-circuit is DB-backed since L4: an org-wide grant row
    (property_id NULL), visible only under the active org's RLS — an
    org_admin of tenant A resolves to nothing while active in tenant B
    because B's walls show none of A's grant rows. Token roles decide
    nothing here."""
    rows = session.execute(
        select(RoleAssignment.property_id, RoleAssignment.department_id).where(
            RoleAssignment.keycloak_subject == principal.subject
        )
    ).all()
    if any(p is None for p, _ in rows):  # org-wide grant → every property
        return Scope(all_properties=True, properties=frozenset(), departments=frozenset())
    if principal.scopes is not None:  # claim present → authoritative, but org-fenced
        return _scope_from_claim(principal.scopes, session)
    return _build_scope((p, d) for p, d in rows if p is not None)


def assignment_scope(principal: Principal, session: Session) -> Scope:
    """The caller's scope from their OWN (property, department) assignments —
    WITHOUT the org-wide short-circuit `resolve_scope` applies (org-wide
    grant rows are deliberately EXCLUDED here). Used for write-confinement:
    a co-held org-wide VIEW grant (e.g. accountant) must not grant
    onboarding authority over a property the caller isn't assigned to."""
    if principal.scopes is not None:
        # Same org-fence as resolve_scope (L8-F5): a realm-global claim naming
        # a foreign-org property must not confer write authority over it.
        return _scope_from_claim(principal.scopes, session)
    rows = session.execute(
        select(RoleAssignment.property_id, RoleAssignment.department_id).where(
            RoleAssignment.keycloak_subject == principal.subject,
            RoleAssignment.property_id.is_not(None),
        )
    ).all()
    return _build_scope((p, d) for p, d in rows if p is not None)


def require_property_access(
    request: Request,
    property_id: Annotated[str, Query(alias="property")],
    principal: Principal = Depends(require_operator),
) -> Principal:
    """403 unless the caller's scope allows the requested `property`. Resolves
    scope via a short-lived session from the app's factory (hybrid: claim or DB).

    KNOWN GAP, left alone deliberately. This asks about SCOPE only, and a
    global-property role (org_admin, accountant) is in scope for every id there
    is — including another tenant's, which RLS then empties, so some endpoints
    behind this door answer "nothing here" where the truth is "not yours".
    `_require_readable_property` is the fix and the workforce reads use it, but
    applying it HERE also converts every unknown property into a 403 across the
    reporting endpoints, where 404 is a deliberate, tested choice ("a missing
    resource, not a bad request" — test_cpa_pack_unknown_property_is_404). That
    is a product decision about the whole portal's error vocabulary, not a
    workforce bug, so it is written down rather than quietly changed."""
    factory = request_session_factory(request)
    with factory() as session:
        scope = resolve_scope(principal, session)
    if not scope.allows_property(property_id):
        raise _refuse_property()
    return principal


router = APIRouter(prefix="/api")


class EmployeeModel(BaseModel):
    employee_id: int
    # From the PRIMARY assignment. None when the employee has no effective
    # placement — terminated, or not yet started. Nullable since E1: property is
    # no longer an intrinsic attribute of a person, it is an attribute of where
    # and when they are placed.
    property_id: str | None
    department_id: int | None
    full_name: str
    pay_type: str
    # D2: GM-maintained scheduling aid, operator-visible (never money/medical).
    # Written via PUT /api/schedule/employees/{id}/availability-note.
    availability_note: str | None = None
    # E3 classification/compliance. `employment_status` and `full_part_time`
    # are operational (who can be scheduled, for how much) and stay visible to
    # every operator in scope. The COMPLIANCE trio below is HR data with no
    # supervisor use — an hourly department manager has no need for I-9/W-4
    # tracking or payroll readiness — so it serializes as null outside the
    # tier that writes it (org_admin/property_gm) plus payroll_admin: the same
    # need-to-know instinct that keeps compensation payroll_admin-gated, one
    # notch lighter because these are dates, not money. Written via
    # PATCH /api/employees/{id}; the artifacts themselves are blocked on the
    # I-9 retention question (BACKLOG).
    employment_status: str = "active"
    full_part_time: str | None = None
    i9_submitted_on: str | None = None
    w4_submitted_on: str | None = None
    payroll_data_complete: bool | None = None


def _last_placement(session: Session, employee_id: int) -> EmployeeAssignment | None:
    """The most recent placement, IGNORING dates and status — the only handle on
    someone who is no longer placed anywhere. Used to scope the inactive list:
    a terminated employee stays visible to the property that employed them, and
    to nobody else."""
    return session.execute(
        select(EmployeeAssignment)
        .where(EmployeeAssignment.employee_id == employee_id)
        .order_by(EmployeeAssignment.effective_from.desc(), EmployeeAssignment.assignment_id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


@router.get("/employees")
def list_employees(
    request: Request,
    principal: Principal = Depends(require_operator),
    include_inactive: Annotated[bool, Query()] = False,
) -> list[EmployeeModel]:
    """Employees visible to the caller's scope: all (global roles), the caller's
    properties (GM), or the caller's departments (department manager).

    `include_inactive` additionally returns people with no placement in force —
    suspended and terminated. They are scoped by their LAST placement rather
    than an effective one (they have none, which is the point), so a terminated
    employee remains visible to the property that employed them and to nobody
    else.
    """
    with _session(request) as session:
        scope = resolve_scope(principal, session)
        employees = session.execute(select(Employee)).scalars().all()
        today = date.today()

        def _in_scope(e: Employee) -> bool:
            # Visible if ANY assignment falls in the caller's scope. A department
            # manager at one hotel must see a shared employee even though that
            # person's primary assignment is the other hotel.
            # An assignment with a NULL department still confers PROPERTY
            # visibility. Someone with no effective assignment is visible to
            # nobody, which is correct: they are not currently placed anywhere.
            placements = [
                (a.property_id, a.department_id)
                for a in assignments_on(session, e.employee_id, today)
            ]
            return any(
                scope.allows_property(property_id)
                or (
                    department_id is not None
                    and scope.allows_department(property_id, department_id)
                )
                for property_id, department_id in placements
            )

        sees_compliance = bool(
            effective_roles(session, principal.subject)
            & {ORG_ADMIN, PROPERTY_GM, PAYROLL_ADMIN}
        )

        def _in_scope_historically(e: Employee) -> bool:
            # The inactive list's scope check. Same question, asked of the last
            # placement instead of an effective one — an employee with none is
            # precisely who this branch exists to show.
            last = _last_placement(session, e.employee_id)
            if last is None:
                return False
            return scope.allows_property(last.property_id) or (
                last.department_id is not None
                and scope.allows_department(last.property_id, last.department_id)
            )
        out: list[EmployeeModel] = []
        for e in employees:
            active = _in_scope(e)
            if not active and not (include_inactive and _in_scope_historically(e)):
                continue
            # The list still shows ONE property/department per row — the primary,
            # i.e. where their paycheck comes from. Someone working two hotels
            # appears once, under the one that pays them; the full placement set
            # lives on the employee detail view.
            primary = primary_assignment_on(session, e.employee_id, today)
            # An inactive employee has no effective placement, so fall back to
            # the last one — otherwise the row shows a blank property and the UI
            # cannot group it under the hotel it belongs to.
            placement = primary if primary is not None else _last_placement(session, e.employee_id)
            out.append(EmployeeModel(
                employee_id=e.employee_id,
                property_id=placement.property_id if placement else None,
                department_id=placement.department_id if placement else None,
                full_name=e.full_name, pay_type=e.pay_type,
                availability_note=e.availability_note,
                employment_status=e.employment_status,
                full_part_time=e.full_part_time,
                i9_submitted_on=(
                    e.i9_submitted_on.isoformat()
                    if sees_compliance and e.i9_submitted_on else None
                ),
                w4_submitted_on=(
                    e.w4_submitted_on.isoformat()
                    if sees_compliance and e.w4_submitted_on else None
                ),
                payroll_data_complete=(
                    e.payroll_data_complete if sees_compliance else None
                ),
            ))
        return out


require_payroll_admin = require_grants(PAYROLL_ADMIN)

# Who may see and set a PAY RATE. Wider than the payroll-PII vault on purpose:
# a GM hires, and hiring means saying what the hire is paid. That is the whole
# justification, and it is enough on its own.
#
# It did not used to be. This gate once cited GET /api/employees/work — which
# served per-person est_cost to every operator — to argue that withholding the
# rate "bought nothing". That was true, and it was the wrong conclusion: cost
# over hours IS the rate, so the wide endpoint was not a reason to widen this
# gate, it was a hole beside it. The endpoint now gates its money on THESE
# grants, which closes the loop rather than reasoning from it.
#
# The vault (SSN, bank, tax elections) is a DIFFERENT gate and stays
# payroll_admin-only. Being allowed to say what someone earns is not being
# allowed to read their social security number.
require_rate_editor = require_grants(PAYROLL_ADMIN, ORG_ADMIN, PROPERTY_GM)


class CompensationModel(BaseModel):
    employee_id: int
    compensation_note: str | None


@router.get("/employees/{employee_id}/compensation")
def employee_compensation(
    employee_id: int,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> CompensationModel:
    """Payroll-Admin-only: decrypt and return an employee's protected note,
    writing an audit_event for the access (segregation of duties)."""
    with _session(request) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")
        session.add(
            AuditEvent(
                actor_subject=principal.subject,
                action="read_compensation",
                resource_type="employee",
                resource_id=str(employee_id),
            )
        )
        note = employee.compensation_note  # decrypted by EncryptedString on load
        session.commit()
        return CompensationModel(employee_id=employee_id, compensation_note=note)


class PayRateModel(BaseModel):
    employee_id: int
    pay_rate: Decimal | None


class SetPayRateBody(BaseModel):
    # A sane hourly-wage envelope; the real guard against fat-finger errors.
    # decimal_places=2: rates are dollars-and-cents — sub-cent precision is a typo.
    pay_rate: Decimal = Field(gt=0, le=10000, decimal_places=2)
    # When the new rate takes effect. Defaults to today, which is the honest
    # default: a rate entered now with no stated date owns no day already
    # worked. Backdating over the current rate's own start is refused.
    effective_from: date | None = None


def _require_rate_access(session: Session, principal: Principal, employee_id: int) -> None:
    """Confinement for the widened rate gate.

    `payroll_admin` and `org_admin` are global-property roles and pass. A
    `property_gm` is confined to its own property — WITHOUT this, opening the
    gate to GMs would have let a GM at one hotel read and rewrite a rate at the
    other, a bigger hole than the one being opened. Scope comes from
    `assignment_scope` (the caller's OWN placements), never `resolve_scope`,
    which would hand a co-held global VIEW role write authority it never had.
    """
    if effective_roles(session, principal.subject) & {PAYROLL_ADMIN, ORG_ADMIN}:
        return
    scope = assignment_scope(principal, session)
    placements = property_ids_on(session, employee_id, date.today())
    if not placements:
        # Nothing to scope the check against. Fail closed — the same reasoning
        # that made `terminate` refuse an employee with no active assignment.
        raise HTTPException(
            status_code=403, detail="employee is not currently placed in your scope"
        )
    if not all(scope.allows_property(p) for p in placements):
        raise HTTPException(status_code=403, detail="property out of scope")


@router.get("/employees/{employee_id}/pay-rate")
def get_pay_rate(
    employee_id: int,
    request: Request,
    principal: Principal = Depends(require_rate_editor),
) -> PayRateModel:
    """Decrypt and return an employee's hourly rate, audited. Open to
    payroll_admin, org_admin, and a property_gm within its own property."""
    with _session(request) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")
        _require_rate_access(session, principal, employee_id)
        session.add(AuditEvent(
            actor_subject=principal.subject, action="read_pay_rate",
            resource_type="employee", resource_id=str(employee_id),
        ))
        assignment = _primary_placement(session, employee_id)
        rate = (
            None if assignment is None
            else rate_on(
                session, assignment.assignment_id, "regular", date.today()
            )
        )
        session.commit()
        return PayRateModel(employee_id=employee_id, pay_rate=rate)


@router.put("/employees/{employee_id}/pay-rate")
def set_pay_rate(
    employee_id: int,
    body: SetPayRateBody,
    request: Request,
    principal: Principal = Depends(require_rate_editor),
) -> PayRateModel:
    """Set an employee's hourly rate (encrypted), audited. Same gate as the read:
    payroll_admin, org_admin, or a property_gm within its own property."""
    with _session(request) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")
        _require_rate_access(session, principal, employee_id)
        assignment = _primary_placement(session, employee_id)
        if assignment is None:
            raise HTTPException(
                status_code=422,
                detail="employee has no primary placement to attach a rate to",
            )

        # THE ORDINARY RAISE: close the open rate, open the next one from
        # `effective_from`. This is why the partial unique index is scoped
        # `WHERE effective_to IS NULL` -- a closed row beside an open one is the
        # normal state, and only two SIMULTANEOUSLY-open rates are forbidden.
        #
        # Overwriting the open row instead would restore the exact bug E2 exists
        # to kill: the row is what already-worked days resolve through, so
        # editing it re-prices a closed period on the next re-promote.
        effective = body.effective_from or date.today()
        open_rate = session.execute(
            select(AssignmentRate).where(
                AssignmentRate.assignment_id == assignment.assignment_id,
                AssignmentRate.rate_type == "regular",
                AssignmentRate.effective_to.is_(None),
            )
        ).scalar_one_or_none()
        if open_rate is not None:
            if open_rate.effective_from >= effective:
                # Closing it would make effective_to <= effective_from: an
                # interval no date predicate can reason about, and the partial
                # index would then permit a second open row silently.
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "the new rate must start after the current one; "
                        "backdating a rate would restate a closed period"
                    ),
                )
            open_rate.effective_to = effective
        session.add(AssignmentRate(
            assignment_id=assignment.assignment_id,
            rate_type="regular",
            amount=str(body.pay_rate),  # EncryptedString encrypts on flush
            effective_from=effective,
        ))
        session.add(AuditEvent(
            actor_subject=principal.subject, action="write_pay_rate",
            resource_type="employee", resource_id=str(employee_id),
        ))
        session.commit()
        return PayRateModel(employee_id=employee_id, pay_rate=body.pay_rate)


def _primary_placement(
    session: Session, employee_id: int
) -> EmployeeAssignment | None:
    """The paycheck-issuing placement, today.

    This endpoint predates E2 and speaks employee-to-one-rate, which no longer
    describes the model: a person can hold two placements paid differently. It
    is scoped to the PRIMARY placement -- the one that issues the paycheck -- so
    the contract it already had stays true rather than becoming ambiguous. A
    per-placement rate surface is a UI change for a later phase; quietly picking
    an arbitrary placement here would have been the silent version of it.
    """
    return primary_assignment_on(session, employee_id, date.today())


require_onboarder = require_grants(ORG_ADMIN, PROPERTY_GM)


def _property_is_here(session: Session, property_id: str) -> bool:
    """Is there such a property IN THE ACTIVE ORG? Asked of an org-bound
    session, so RLS answers it: another tenant's property is not merely
    out of scope, it is not there."""
    return session.execute(
        select(Property.property_id).where(Property.property_id == property_id)
    ).scalar_one_or_none() is not None


def _refuse_property() -> HTTPException:
    """One refusal for out-of-scope, another org's, and nonexistent alike.
    Which of the three it was is not the caller's business — the same
    reasoning `require_grants` applies to which door refused, and the same
    reasoning behind the kiosk's identical 403 for an unknown vs a denied
    employee."""
    return HTTPException(status_code=403, detail="property out of scope")


def _require_onboardable_property(
    session: Session, principal: Principal, property_id: str
) -> None:
    """403 unless `property_id` is one this caller may create records under.

    Both branches matter, and the org_admin one is the branch that was missing.
    A property_gm is confined by its assignment scope — the check that was
    always here. An org_admin has no assignment scope by design (it holds the
    whole org), which read as "no check to run"; but the property id arrives in
    the request body, so unchecked it names ANY property, including another
    tenant's.
    """
    if not _property_is_here(session, property_id):
        raise _refuse_property()
    if ORG_ADMIN in effective_roles(session, principal.subject):
        return
    if not assignment_scope(principal, session).allows_property(property_id):
        raise _refuse_property()


def _require_readable_property(session: Session, scope: Scope, property_id: str) -> None:
    """403 unless the caller may READ this property's workforce records.

    Two questions, and the second one is easy to lose. `scope.allows_property`
    answers "is this within the caller's remit", and for a global-property role
    (org_admin, accountant) the answer is yes to EVERY id — including one that
    belongs to another tenant. RLS then filters the rows away and the endpoint
    returns 200 with an empty list, which is a refusal wearing the costume of
    an answer: the client renders "no departments" and nobody learns the
    request was denied. So ask the second question too.
    """
    if not scope.allows_property(property_id) or not _property_is_here(session, property_id):
        raise _refuse_property()


class DepartmentModel(BaseModel):
    department_id: int
    property_id: str
    name: str


class DepartmentCreateBody(BaseModel):
    property_id: str
    name: str


@router.get("/departments")
def list_departments(
    request: Request,
    property_id: Annotated[str, Query(alias="property")],
    principal: Principal = Depends(require_operator),
) -> list[DepartmentModel]:
    """Departments of one property, name-sorted. Property-confined like every
    workforce read: an out-of-scope property is a 403, not an empty list."""
    with _session(request) as session:
        scope = resolve_scope(principal, session)
        _require_readable_property(session, scope, property_id)
        rows = (
            session.execute(
                select(Department)
                .where(Department.property_id == property_id)
                .order_by(Department.name)
            )
            .scalars()
            .all()
        )
        return [
            DepartmentModel(
                department_id=d.department_id, property_id=d.property_id, name=d.name
            )
            for d in rows
        ]


@router.post("/departments", status_code=201)
def create_department(
    body: DepartmentCreateBody,
    request: Request,
    principal: Principal = Depends(require_onboarder),
) -> DepartmentModel:
    """Create a department. Same gate as onboarding (org_admin / property_gm,
    GM confined to its own property); duplicate name in a property is a 409."""
    name = body.name.strip()
    if name == "":
        raise HTTPException(status_code=422, detail="name must not be empty")
    with _session(request) as session:
        _require_onboardable_property(session, principal, body.property_id)
        exists = session.execute(
            select(Department).where(
                Department.property_id == body.property_id, Department.name == name
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise HTTPException(status_code=409, detail=f"department {name!r} already exists")
        dept = Department(property_id=body.property_id, name=name)
        session.add(dept)
        session.commit()
        return DepartmentModel(
            department_id=dept.department_id, property_id=dept.property_id, name=dept.name
        )



def _kc_admin(request: Request) -> KeycloakAdmin:
    admin: KeycloakAdmin = request.app.state.keycloak_admin
    return admin


class OnboardBody(BaseModel):
    full_name: str
    email: str | None = None
    property_id: str = Field(alias="property")
    pay_type: str
    role: str | None = None
    department_id: int | None = None
    position_id: int | None = None
    compensation_note: str | None = None


class OnboardedModel(BaseModel):
    employee_id: int
    keycloak_subject: str | None
    property_id: str
    full_name: str


class MeModel(BaseModel):
    subject: str
    username: str
    roles: list[str]


@router.get("/me")
def me(principal: Principal = Depends(require_operator)) -> MeModel:
    """The caller's identity + roles — the SPA gates admin UI on this.

    `roles` reports the TOKEN's realm roles, which since L4 can EXCEED the
    caller's effective in-org grants (realm roles are realm-global; org
    authority is the role_assignment grants read under the active org). So
    the SPA may render admin UI here whose actions then 403 in an org
    where the caller holds no grant — reconciled in L6, when /me can open
    a session and report effective (grant-backed) roles."""
    return MeModel(subject=principal.subject, username=principal.username,
                   roles=sorted(principal.roles))


@router.post("/employees", status_code=201)
def onboard(
    body: OnboardBody, request: Request,
    principal: Principal = Depends(require_onboarder),
) -> OnboardedModel:
    """Onboard an operator (provision Keycloak + role_assignment) or record an
    hourly employee. A property_gm may only onboard into its own property.
    An operator role REQUIRES an email (it provisions a login)."""
    violation = pay_type_violation(body.pay_type)
    if violation is not None:
        raise HTTPException(status_code=422, detail=violation)
    if body.role in OPERATOR_ROLES and body.email is None:
        raise HTTPException(status_code=422, detail="email is required for an operator role")
    if body.role == DEPARTMENT_MANAGER and body.department_id is None:
        raise HTTPException(
            status_code=422, detail="department_id is required for a department_manager"
        )
    with _session(request) as session:
        _require_onboardable_property(session, principal, body.property_id)
        employee = onboard_employee(
            session, _kc_admin(request),
            OnboardRequest(
                full_name=body.full_name, email=body.email, property_id=body.property_id,
                pay_type=body.pay_type, role=body.role, department_id=body.department_id,
                position_id=body.position_id, compensation_note=body.compensation_note,
            ),
            actor_subject=principal.subject,
        )
        session.commit()
        return OnboardedModel(
            employee_id=employee.employee_id, keycloak_subject=employee.keycloak_subject,
            property_id=body.property_id, full_name=employee.full_name,
        )


class EmployeeWorkModel(BaseModel):
    """One person's worked hours and estimated cost over a window.

    `est_cost` is None when the caller may not see money on this axis — the
    same "null means withheld, not zero" convention the statement's department
    suppression uses, so a UI that already renders an em dash for a suppressed
    figure needs no new vocabulary."""

    employee_id: int
    hours: Decimal
    ot_hours: Decimal
    est_cost: Decimal | None


# How far back one call may reach. Same 400 as `labor_analytics`, and for the
# same reason: a caller-chosen window is a differencing instrument. Two reads
# that straddle a boundary subtract to the figure for the days between them, so
# an uncapped window lets a caller assemble any slice they like out of legal
# requests. Capping does not stop differencing — nothing does — but it puts a
# floor under the number of audited reads a fishing expedition costs.
_WORK_WINDOW_MAX_DAYS = 400


@router.get("/employees/work")
def employee_work(
    request: Request,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    principal: Principal = Depends(require_operator),
    property_id: Annotated[str | None, Query(alias="property")] = None,
) -> list[EmployeeWorkModel]:
    """Per-employee hours and estimated cost from promoted labor facts.

    NOTE ON DISCLOSURE. `usali_labor_fact` is a DEPARTMENT-level aggregate
    precisely so an individual's pay rate cannot be re-derived as cost / hours;
    this endpoint decomposes it back to the person. HOURS are the operational
    fact and go to every operator who can already see the employee on the
    roster — a department manager cannot run a department without them.

    MONEY is gated, because `est_cost / hours` IS the effective pay rate, and
    that is exactly what `require_rate_editor` withholds from a department
    manager and an accountant at `/pay-rate`. Serving it here would have made
    that gate ornamental: the same figure, one division away, through a door
    with a wider lock. So `est_cost` carries the rate-editor grants, and
    everyone else gets None beside a real `hours`.

    Every read is audited, and a read that DISCLOSED money additionally leaves
    one row per person whose earnings were shown — so the trail answers "who
    looked at Ada's pay", not merely "somebody read this property".

    Scope still applies in full — an employee outside the caller's scope is
    absent from the result, not zeroed.
    """
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if (date_to - date_from).days > _WORK_WINDOW_MAX_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"window must be {_WORK_WINDOW_MAX_DAYS} days or fewer",
        )
    with _session(request) as session:
        scope = resolve_scope(principal, session)
        if property_id is not None:
            _require_readable_property(session, scope, property_id)
        rows = session.execute(
            select(
                Timecard.employee_id,
                UsaliLaborFact.property_id,
                UsaliLaborFact.department_id,
                func.sum(UsaliLaborFact.hours),
                func.sum(UsaliLaborFact.ot_hours),
                func.sum(UsaliLaborFact.est_cost),
            )
            .join(Timecard, Timecard.timecard_id == UsaliLaborFact.timecard_id)
            .where(
                UsaliLaborFact.business_date >= date_from,
                UsaliLaborFact.business_date <= date_to,
                *([UsaliLaborFact.property_id == property_id] if property_id else []),
            )
            .group_by(
                Timecard.employee_id, UsaliLaborFact.property_id, UsaliLaborFact.department_id
            )
        ).all()

        totals: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
        for employee_id, fact_property, department_id, hours, ot_hours, cost in rows:
            # Scope is checked per FACT, not per employee: the facts carry the
            # property and department the hours were worked in, so a department
            # manager sees the hours their own department bought and not the
            # ones the same person worked elsewhere.
            allowed = scope.allows_property(fact_property) or (
                department_id is not None
                and scope.allows_department(fact_property, department_id)
            )
            if not allowed:
                continue
            h, o, c = totals.get(employee_id, (Decimal(0), Decimal(0), Decimal(0)))
            totals[employee_id] = (
                h + Decimal(str(hours)), o + Decimal(str(ot_hours)), c + Decimal(str(cost))
            )
        # Does this caller see money? Asked of the GRANTS, not the token, and
        # asked here rather than as a Depends() because it selects a FIELD
        # rather than the door — `require_rate_editor` would 403 the whole
        # read, and a department manager is entitled to the hours.
        sees_cost = bool(
            effective_roles(session, principal.subject)
            & {PAYROLL_ADMIN, ORG_ADMIN, PROPERTY_GM}
        )
        # The window is part of what was read: "read HISJ" cannot be told apart
        # from "read HISJ for one day" after the fact, and a differencing sweep
        # is visible only in the SEQUENCE of windows. 27 chars at the widest,
        # well inside resource_id's 64.
        session.add(AuditEvent(
            actor_subject=principal.subject, action="read_employee_work",
            resource_type="property",
            resource_id=f"{property_id or '*'}:{date_from.isoformat()}:{date_to.isoformat()}",
        ))
        if sees_cost:
            # A money read is a per-person disclosure, so it leaves a per-person
            # trail — the shape every other individual-record audit in this
            # codebase uses (kiosk_my_week, the deposit-account writes). An
            # hours-only read leaves the window row alone: auditing it per
            # person would bury the disclosures under the routine traffic.
            session.add_all(
                AuditEvent(
                    actor_subject=principal.subject, action="read_employee_earnings",
                    resource_type="employee", resource_id=str(employee_id),
                )
                for employee_id in sorted(totals)
            )
        session.commit()
        return [
            EmployeeWorkModel(
                employee_id=k, hours=v[0], ot_hours=v[1],
                est_cost=v[2] if sees_cost else None,
            )
            for k, v in sorted(totals.items())
        ]


class TerminatedModel(BaseModel):
    employee_id: int
    termination_date: str | None


@router.post("/employees/{employee_id}/terminate")
def terminate(
    employee_id: int, request: Request,
    principal: Principal = Depends(require_onboarder),
) -> TerminatedModel:
    """Disable the employee's Keycloak user and mark the record terminated."""
    with _session(request) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")
        if ORG_ADMIN not in effective_roles(session, principal.subject):
            scope = assignment_scope(principal, session)
            # Terminating ends employment everywhere, so the caller must have
            # scope over EVERY property this person is assigned to. Scope over
            # one hotel must not let a manager end someone's job at the other.
            placements = property_ids_on(session, employee_id, date.today())
            if not placements:
                # No effective assignment: already terminated, or not started.
                # There is nothing to scope the check against, and guessing let a
                # GM scoped to ONE property terminate an employee belonging to
                # another (reproduced: HTTP 200).
                raise HTTPException(
                    status_code=409,
                    detail="employee has no active assignment to terminate",
                )
            if not all(scope.allows_property(p) for p in placements):
                raise HTTPException(status_code=403, detail="property out of scope")
        terminate_employee(session, _kc_admin(request), employee_id,
                           actor_subject=principal.subject, on_date=date.today(),
                           # Separation deletes any face template AND its
                           # reference photo (F3) — the route always has the
                           # store in hand, so the photo can't be orphaned.
                           photo_store=request.app.state.photo_store)
        session.commit()
        emp = session.get(Employee, employee_id)
        assert emp is not None
        return TerminatedModel(
            employee_id=employee_id,
            termination_date=emp.termination_date.isoformat() if emp.termination_date else None,
        )



def _move_department(
    session: Session, employee_id: int, department_id: int, effective: date
) -> None:
    """Move an employee to another department, EFFECTIVE-DATED.

    A department move is not an update. `EmployeeAssignment.department_id` is
    what `department_at` resolves when labor hours are attributed, so editing
    it in place would move hours ALREADY promoted under the old department — a
    re-promote of a closed period would land them somewhere else and Schedule
    14 would stop tying to what was filed. So the move is written the way a
    raise is: close the current placement at the effective date, open a new one
    from it.

    The open pay rate is CARRIED FORWARD onto the new placement, because a rate
    hangs off the placement and a move that silently un-prices someone is worse
    than either outcome it could be mistaken for. Carrying it is not a rate
    change: the amount is identical, and the payroll gate still owns changing it.
    """
    current = primary_assignment_on(session, employee_id, date.today())
    if current is None:
        raise HTTPException(
            status_code=409, detail="employee has no primary placement to move"
        )
    if current.department_id == department_id:
        return
    department = session.get(Department, department_id)
    if department is None or department.property_id != current.property_id:
        raise HTTPException(
            status_code=422,
            detail="department does not belong to this employee's property",
        )
    if current.effective_from >= effective:
        # Same interval rule as a raise: closing at or before the start would
        # make a range no date predicate can reason about.
        raise HTTPException(
            status_code=422,
            detail="the move must start after the current placement began; "
                   "backdating it would restate days already worked",
        )
    open_rate = session.execute(
        select(AssignmentRate).where(
            AssignmentRate.assignment_id == current.assignment_id,
            AssignmentRate.rate_type == "regular",
            AssignmentRate.effective_to.is_(None),
        )
    ).scalar_one_or_none()
    carried = open_rate.amount if open_rate is not None else None
    current.effective_to = effective
    # Flush the close BEFORE the insert: uq_one_active_primary_per_employee is
    # partial on (is_primary, status='active', effective_to IS NULL), so two
    # open primaries exist for an instant otherwise.
    session.flush()
    moved = EmployeeAssignment(
        employee_id=employee_id,
        property_id=current.property_id,
        department_id=department_id,
        position_id=current.position_id,
        is_primary=current.is_primary,
        status="active",
        effective_from=effective,
    )
    session.add(moved)
    session.flush()
    if carried is not None:
        session.add(AssignmentRate(
            assignment_id=moved.assignment_id,
            rate_type="regular",
            amount=carried,  # re-encrypted on flush; the same figure
            effective_from=effective,
        ))



_PATCHABLE_STATUSES = frozenset({"active", "inactive", "leave"})
_FULL_PART_TIME = frozenset({"full_time", "part_time"})


class EmployeePatchBody(BaseModel):
    employment_status: str | None = None
    full_part_time: str | None = None
    i9_submitted_on: date | None = None
    w4_submitted_on: date | None = None
    payroll_data_complete: bool | None = None
    # Identity and placement, edited from the roster's Edit action. These are
    # NOT classification fields and are applied separately below —
    # `department_id` in particular is a MOVE, not an update.
    full_name: str | None = None
    pay_type: str | None = None
    department_id: int | None = None
    # When a department move takes effect. Defaults to today, for the same
    # reason a rate change does: a move entered now owns no day already worked.
    effective_from: date | None = None


class EmployeeClassificationModel(BaseModel):
    employee_id: int
    full_name: str
    pay_type: str
    employment_status: str
    full_part_time: str | None
    i9_submitted_on: str | None
    w4_submitted_on: str | None
    payroll_data_complete: bool


@router.patch("/employees/{employee_id}")
def patch_employee(
    employee_id: int, body: EmployeePatchBody, request: Request,
    principal: Principal = Depends(require_onboarder),
) -> EmployeeClassificationModel:
    """Update classification/compliance fields. Only fields PRESENT in the
    request are touched (an omitted field is not a null write).

    `terminated` is refused in BOTH directions. Entering it here would
    terminate someone WITHOUT closing their assignments or running the
    stranded-day refusal — use POST .../terminate, the one writer of that
    status. Leaving it is a rehire, which reopens Keycloak, assignments, and
    rates together and is its own flow, not a field write.

    Post-termination paperwork (the late W-4) is ORG_ADMIN work: termination
    closes every placement, so a scoped GM has nothing left to scope the edit
    against and gets the 409 below — deliberate, because with no placements
    there is no way to verify the caller's authority over this person.
    """
    fields = body.model_dump(exclude_unset=True)
    if "employment_status" in fields:
        wanted = fields["employment_status"]
        if wanted == "terminated":
            raise HTTPException(
                status_code=422,
                detail="employment_status cannot be set to terminated here; "
                       "use POST /api/employees/{id}/terminate, which also "
                       "closes the employee's assignments",
            )
        if wanted not in _PATCHABLE_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"employment_status must be one of "
                       f"{sorted(_PATCHABLE_STATUSES)}",
            )
    if fields.get("full_part_time") is not None and (
        fields["full_part_time"] not in _FULL_PART_TIME
    ):
        raise HTTPException(
            status_code=422,
            detail=f"full_part_time must be one of {sorted(_FULL_PART_TIME)} or null",
        )
    if fields.get("full_name") is not None and fields["full_name"].strip() == "":
        raise HTTPException(status_code=422, detail="full_name must not be empty")
    if fields.get("pay_type") is not None and fields["pay_type"] not in {"hourly", "salary"}:
        raise HTTPException(status_code=422, detail="pay_type must be hourly or salary")
    if "payroll_data_complete" in fields and fields["payroll_data_complete"] is None:
        # The column is NOT NULL by design — "unknown" is not a completeness
        # state; refuse here rather than let the constraint 500.
        raise HTTPException(
            status_code=422, detail="payroll_data_complete must be true or false"
        )
    with _session(request) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")
        if (
            "employment_status" in fields
            and employee.employment_status == "terminated"
        ):
            raise HTTPException(
                status_code=422,
                detail="employee is terminated; rehire is not a status write",
            )
        if ORG_ADMIN not in effective_roles(session, principal.subject):
            # Same rule as terminate: these fields are property-agnostic facts
            # about the person, so the caller must hold scope over EVERY
            # property the person is placed at — scope over one hotel must not
            # edit the record the other hotel's kiosk and pay run read.
            scope = assignment_scope(principal, session)
            placements = property_ids_on(session, employee_id, date.today())
            if not placements:
                raise HTTPException(
                    status_code=409,
                    detail="employee has no active assignment to scope the "
                           "edit against",
                )
            if not all(scope.allows_property(p) for p in placements):
                raise HTTPException(status_code=403, detail="property out of scope")
        # The placement fields never reach setattr: `department_id` is not a
        # column on Employee, and a move is a new placement rather than an
        # edit to the current one (see _move_department).
        effective = fields.pop("effective_from", None) or date.today()
        move_to = fields.pop("department_id", None)
        if fields.get("full_name") is not None:
            fields["full_name"] = fields["full_name"].strip()
        for name, value in fields.items():
            setattr(employee, name, value)
        if move_to is not None:
            _move_department(session, employee_id, move_to, effective)
        session.add(AuditEvent(
            actor_subject=principal.subject, action="update_employee",
            resource_type="employee", resource_id=str(employee_id),
        ))
        session.flush()
        result = EmployeeClassificationModel(
            employee_id=employee_id,
            full_name=employee.full_name,
            pay_type=employee.pay_type,
            employment_status=employee.employment_status,
            full_part_time=employee.full_part_time,
            i9_submitted_on=(
                employee.i9_submitted_on.isoformat()
                if employee.i9_submitted_on else None
            ),
            w4_submitted_on=(
                employee.w4_submitted_on.isoformat()
                if employee.w4_submitted_on else None
            ),
            payroll_data_complete=employee.payroll_data_complete,
        )
        session.commit()
        return result
