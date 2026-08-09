"""Operator onboarding/termination service (Pillar A2.3).

Pure functions over a Session + a KeycloakAdmin, so the endpoints stay thin and
the loop is unit-tested with the in-memory fake. Onboarding an OPERATOR (a role
that logs in) provisions a Keycloak user and writes the authoritative
`role_assignment` (which A2.2's `resolve_scope` reads). An hourly employee
(no role) is recorded with `keycloak_subject = NULL` — the passwordless kiosk
(Pillar B) handles their identity later.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from usali.auth import DEPARTMENT_MANAGER, OPERATOR_ROLES, ORG_WIDE_ROLES
from usali.keycloak_admin import KeycloakAdmin, KeycloakAdminConflict
from usali.models import (
    PAY_TYPES,
    AuditEvent,
    Employee,
    EmployeeAssignment,
    EmployeeFaceTemplate,
    RoleAssignment,
    Timecard,
    UsaliLaborFact,
)
from usali.photo_store import PhotoStore, face_reference_key
from usali.tenancy import current_org_id


def pay_type_violation(value: str) -> str | None:
    """None if `value` is a legal pay type, else the refusal message.

    The message WITHHOLDS the submitted value (roster_seed's lesson, which is
    the C2 lesson: a column-shifted paste puts an SSN where a pay type goes,
    and refusal messages reach logs and API clients). One function so the API
    door and the service door cannot drift on what the closed set is.
    """
    if value in PAY_TYPES:
        return None
    return (
        f"pay_type must be one of {sorted(PAY_TYPES)} "
        "(value withheld from this message)"
    )


@dataclass(frozen=True)
class OnboardRequest:
    full_name: str
    email: str | None
    property_id: str
    pay_type: str
    role: str | None = None
    department_id: int | None = None
    position_id: int | None = None
    compensation_note: str | None = None


def onboard_employee(
    session: Session, kc: KeycloakAdmin, req: OnboardRequest, *, actor_subject: str
) -> Employee:
    """Create the employee record; if `role` is an operator role, provision a
    Keycloak user + write the role_assignment. Records an audit event. The caller
    commits."""
    violation = pay_type_violation(req.pay_type)
    if violation is not None:
        # Before any external side effect: a Keycloak user provisioned for a
        # row that then fails to insert is the orphan-account problem below.
        raise ValueError(violation)
    provision = req.role in OPERATOR_ROLES and req.email is not None
    subject: str | None = None
    if provision:
        assert req.email is not None  # narrowed by `provision`
        username = req.email.split("@")[0]
        # Look before creating. create_user is a NON-TRANSACTIONAL external side
        # effect: if it succeeds and this session later rolls back, the realm
        # account survives with no employee row and no role_assignment. Since
        # org_admin/accountant/payroll_admin derive scope from the realm role
        # alone, that orphan is an all-properties account invisible to every
        # DB-driven view and unreachable by terminate_employee. Reusing an
        # existing account on re-run is what makes a partial failure recoverable.
        existing = kc.find_user_by_username(username)
        if existing is not None:
            subject, existing_email = existing
            if existing_email.lower() != req.email.lower():
                raise KeycloakAdminConflict(
                    f"realm username {username!r} already belongs to a different "
                    f"person; {req.email!r} would silently inherit their identity "
                    "and roles. Give one of them a distinct username."
                )
            # Re-map the role on adoption (idempotent): a prior run that created
            # the user but died before create_user's role-mapping step would
            # otherwise leave this operator without even the coarse role.
            kc.assign_realm_roles(subject, [req.role])  # type: ignore[list-item]  # provision => role is not None
        else:
            subject = kc.create_user(
                username=username, email=req.email, full_name=req.full_name,
                realm_roles=[req.role],  # type: ignore[list-item]  # provision => role is not None
            )

    employee = Employee(
        keycloak_subject=subject,
        full_name=req.full_name,
        pay_type=req.pay_type,
        # The day they start IS today on this path -- there is no backdating
        # here yet. Recording it keeps `hire_date` and the assignment's
        # `effective_from` from disagreeing, which is what let the backfill
        # produce assignments predating employment.
        hire_date=date.today(),
        compensation_note=req.compensation_note,
    )
    session.add(employee)
    session.flush()  # need employee_id for the assignment

    # THE placement. Property, department and position live here now, not on the
    # employee row — a person can hold several, at different properties, with
    # different jobs and different rates. is_primary marks the one whose pay run
    # issues their paycheck; a new hire has exactly one, so it is theirs.
    session.add(
        EmployeeAssignment(
            employee_id=employee.employee_id,
            property_id=req.property_id,
            department_id=req.department_id,
            position_id=req.position_id,
            is_primary=True,
            status="active",
            # Same day as hire_date above, deliberately. An assignment starting
            # before the employee was hired resolves them as employed -- and
            # ADMISSIBLE at a kiosk -- before they existed.
            effective_from=date.today(),
        )
    )

    if provision and subject is not None:
        # L6b coherence fix (the L4 deferral, now due): an org-wide role
        # (org_admin/accountant/payroll_admin) gets an ORG-WIDE grant
        # (property_id NULL) — the SAME shape the seeds and provision_tenant
        # write, so an API-onboarded admin and a seeded/provisioned one resolve
        # identically (an org_admin's resolve_scope sees ALL properties, the
        # pre-L4 behavior restored). A property/department role keeps its
        # scoped row: property_gm confined to its property, department_manager
        # to its department. The write wall stamps org_id from the session on
        # the request path.
        if req.role in ORG_WIDE_ROLES:
            prop: str | None = None
            dept: int | None = None
        else:
            prop = req.property_id
            dept = req.department_id if req.role == DEPARTMENT_MANAGER else None
        session.add(
            RoleAssignment(
                keycloak_subject=subject, role=req.role,
                property_id=prop, department_id=dept,
            )
        )

    session.add(
        AuditEvent(
            actor_subject=actor_subject, action="onboard_employee",
            resource_type="employee", resource_id=req.email or req.full_name,
        )
    )
    session.flush()  # populate employee_id for the caller
    return employee


def terminate_employee(
    session: Session, kc: KeycloakAdmin, employee_id: int, *,
    actor_subject: str, on_date: date,
    photo_store: PhotoStore | None = None,
) -> Employee:
    """Disable the employee's Keycloak user (if any), CLOSE every open
    assignment, and mark the record terminated. Raises ValueError if the employee
    does not exist. Caller commits.

    Separation is the retention posture's hard edge (F3): any enrolled face
    template is DELETED here, and — when the caller supplies the photo store,
    as the API route always does — the reference photo with it. The template
    row goes regardless: the biometric must not outlive employment even on a
    path that has no store in hand."""
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise ValueError(f"unknown employee {employee_id}")

    # DO NOT STRAND A FILED DAY. Since E2, labor cost is resolved through the
    # assignment predicate, so closing an assignment before a day that already
    # has a promoted `UsaliLaborFact` makes the next re-promote re-price that day
    # to zero and refile it under no department -- silently restating a Schedule
    # 14 that was already filed, which is the exact harm E2 exists to prevent.
    # `effective_to = on_date + 1` (below) excludes any day strictly after
    # `on_date`, so a filed fact dated later than `on_date` is what gets
    # stranded. Refuse before any state changes rather than warn after.
    stranded = session.execute(
        select(func.min(UsaliLaborFact.business_date))
        .join(Timecard, Timecard.timecard_id == UsaliLaborFact.timecard_id)
        .where(
            Timecard.employee_id == employee_id,
            UsaliLaborFact.business_date > on_date,
        )
    ).scalar_one_or_none()
    if stranded is not None:
        raise ValueError(
            f"cannot terminate employee {employee_id} on {on_date.isoformat()}: "
            f"a labor cost is already filed for {stranded.isoformat()}, which is "
            "after that date. Ending the assignment here would restate that "
            "closed period to zero on the next re-promote. Terminate on or after "
            "the last worked day, or correct the filed facts first."
        )

    if employee.keycloak_subject is not None:
        kc.disable_user(employee.keycloak_subject)
    employee.termination_date = on_date
    # Enum and date move together — ck_employee_terminated_iff_dated refuses
    # either one alone. This is the ONLY writer of `terminated`: it is the one
    # path that closes assignments and runs the stranded-day refusal above, so
    # a status write anywhere else would terminate someone without ending
    # their placements (the split-brain E1 closed).
    employee.employment_status = "terminated"

    # CLOSING THE ASSIGNMENTS IS NOT OPTIONAL. The whole employment model rests
    # on "effective-dating alone answers whether this person was employed here on
    # date X" -- the backfill honours that for pre-existing terminations, but
    # until now the runtime path set only termination_date, leaving assignments
    # open forever. Kiosk paths happened to survive because they re-filter
    # termination_date separately; the pay-run population and every scope query
    # did not, and E1 Task 9 removes that column entirely.
    #
    # effective_to is EXCLUSIVE, so a termination on the last day worked closes
    # the day AFTER, keeping that final day inside the assignment and inside the
    # pay run that must pay for it.
    for assignment in session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == employee_id,
            EmployeeAssignment.status == "active",
        )
    ).scalars():
        assignment.status = "inactive"
        if assignment.effective_to is None or assignment.effective_to > on_date:
            assignment.effective_to = on_date + timedelta(days=1)
    template = session.execute(
        select(EmployeeFaceTemplate).where(
            EmployeeFaceTemplate.employee_id == employee_id
        )
    ).scalar_one_or_none()
    if template is not None:
        session.delete(template)
        if photo_store is not None:
            photo_store.delete(
                face_reference_key(current_org_id(session), employee_id)
            )
        # Audited only when a template actually existed — a deletion record
        # for nothing would be a false entry in the trail.
        session.add(
            AuditEvent(
                actor_subject=actor_subject, action="face_template_deleted",
                resource_type="employee", resource_id=str(employee_id),
            )
        )
    session.add(
        AuditEvent(
            actor_subject=actor_subject, action="terminate_employee",
            resource_type="employee", resource_id=str(employee_id),
        )
    )
    return employee
