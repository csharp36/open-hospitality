"""Create an employee WITH a placement.

E1 Task 9 removed `Employee.property_id` / `department_id` / `position_id`.
Those are attributes of a PLACEMENT — where and when someone works — not of the
person, because a person can work at two hotels doing two different jobs.

Every test that used to write `Employee(property_id=..., department_id=...)`
goes through here instead, so the fixture builds the same shape the production
onboarding path builds. Fixtures that quietly diverge from production are how
the OIDC audience bug survived 700+ green tests.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import AssignmentRate, Employee, EmployeeAssignment

# The payroll anchor: the same effective_from the E1 backfill used, so a test
# employee is placed for any business date the rest of the suite exercises.
DEFAULT_EFFECTIVE_FROM = date(2026, 1, 1)


def make_employee(
    session: Session,
    *,
    property_id: str,
    department_id: int | None = None,
    position_id: int | None = None,
    is_primary: bool = True,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    status: str = "active",
    place_primary: bool = True,
    pay_rate: str | None = None,
    **employee_kwargs: object,
) -> Employee:
    """An `Employee` plus one `EmployeeAssignment`, flushed and ready.

    `place_primary=False` creates the employee with NO assignment, for tests
    that build their own placements. Without it they would hit the partial
    unique index that permits only one open active primary -- which is the index
    doing its job, not a test problem.

    `pay_rate` is a convenience that writes an open `regular` AssignmentRate on
    the placement, effective from the same date. It is NOT an employee attribute
    -- E2 moved rates onto the PLACEMENT -- but the great majority of tests only
    need to say "this person earns $20", and routing that through here keeps the
    fixture building the shape production builds. Tests that care about rate
    DATING should write `AssignmentRate` rows directly.
    """
    # E3 ties the enum to the date with a CHECK constraint, so a fixture that
    # sets one without the other cannot flush. Passing `termination_date` means
    # "this person is terminated" -- say so in both fields, the way
    # `terminate_employee` does, unless the caller explicitly set a status.
    if (
        employee_kwargs.get("termination_date") is not None
        and "employment_status" not in employee_kwargs
    ):
        employee_kwargs["employment_status"] = "terminated"
    # Fixture staff are the backfilled pilot roster: paid today, paperwork in
    # (e3a0 backfills TRUE for the same reason). The MODEL default stays False
    # -- that direction is pinned by the onboarding test -- and tests of the
    # incomplete-blocks-the-run gate pass False explicitly.
    employee_kwargs.setdefault("payroll_data_complete", True)
    employee = Employee(**employee_kwargs)  # type: ignore[arg-type]
    session.add(employee)
    session.flush()
    if not place_primary:
        if pay_rate is not None:
            raise ValueError(
                "pay_rate needs a placement to hang off -- rates belong to an "
                "assignment since E2. Pass it to `place()` instead."
            )
        return employee
    assignment = EmployeeAssignment(
        employee_id=employee.employee_id,
        property_id=property_id,
        department_id=department_id,
        position_id=position_id,
        is_primary=is_primary,
        status=status,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    session.add(assignment)
    session.flush()
    if pay_rate is not None:
        set_rate(session, assignment, pay_rate)
    return employee


def set_rate(
    session: Session,
    assignment: EmployeeAssignment,
    amount: str,
    *,
    rate_type: str = "regular",
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> AssignmentRate:
    """An open rate on a placement, effective from the placement's own start.

    Defaulting to the assignment's `effective_from` matters: a rate that started
    later would resolve to None on the earlier dates, and `None` means "cannot
    cost", so the test would silently assert against $0 rather than the rate it
    named.
    """
    rate = AssignmentRate(
        assignment_id=assignment.assignment_id,
        rate_type=rate_type,
        amount=amount,
        effective_from=effective_from or assignment.effective_from,
        effective_to=effective_to,
    )
    session.add(rate)
    session.flush()
    return rate


def set_department(
    session: Session, employee: Employee, department_id: int | None
) -> None:
    """Move an employee into a department by editing their PRIMARY assignment.

    `employee.department_id = x` is not an error since Task 9 -- SQLAlchemy
    accepts an attribute that maps to no column, so the write silently does
    nothing and the test goes green having asserted nothing. Two tests were
    doing exactly that. Go through here instead.
    """
    primary = session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == employee.employee_id,
            EmployeeAssignment.is_primary.is_(True),
            EmployeeAssignment.effective_to.is_(None),
        )
    ).scalar_one()
    primary.department_id = department_id
    session.flush()


def place(
    session: Session,
    employee: Employee,
    *,
    property_id: str,
    department_id: int | None = None,
    position_id: int | None = None,
    is_primary: bool = False,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    status: str = "active",
    pay_rate: str | None = None,
) -> EmployeeAssignment:
    """Add a SECOND placement — the multi-property case E1 exists for.

    `pay_rate` writes an open `regular` rate on THIS placement. Since E2 that is
    the point: the same person can earn one rate at the front desk and another
    in laundry, so a second placement can carry genuinely different money.
    """
    assignment = EmployeeAssignment(
        employee_id=employee.employee_id,
        property_id=property_id,
        department_id=department_id,
        position_id=position_id,
        is_primary=is_primary,
        status=status,
        effective_from=effective_from,
        effective_to=effective_to,
    )
    session.add(assignment)
    session.flush()
    if pay_rate is not None:
        set_rate(session, assignment, pay_rate)
    return assignment


def set_rate_everywhere(
    session: Session,
    employee: Employee,
    amount: str,
    *,
    rate_type: str = "regular",
) -> None:
    """One open rate on EVERY placement this employee holds.

    For the multi-property tests, which mean "this person earns $20 wherever
    they work". Since E2 that sentence needs a row per placement, and saying it
    once here beats repeating the loop in six seed functions.

    Deliberately NOT the default: the same person earning DIFFERENT money at
    two hotels is the case E2 exists for, and a fixture that silently equalised
    rates would make that case unreachable in tests.
    """
    for assignment in session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == employee.employee_id
        )
    ).scalars():
        set_rate(session, assignment, amount, rate_type=rate_type)
