"""Regressions for the six Criticals a three-lens adversarial review found in E1.

Each test here exists because something shipped that I had reported as done.
The pattern worth naming: I fixed the INSTANCE and wrote a comment explaining the
reasoning, instead of fixing the CLASS and encoding the reasoning in a function.
`has_assignment_rows` is that function; these tests are the fence around it.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from tests.employees import make_employee
from usali.assignments import (
    AmbiguousPrimaryError,
    employee_ids_with_primary_at,
    has_assignment_rows,
    is_exempt_on,
)
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    Department,
    EmployeeAssignment,
    Organization,
    Position,
    Property,
)
from usali.onboarding import terminate_employee
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_LATER = date(2026, 4, 1)


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="58033", org_id=1, name="BW",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="SJCES", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    return dept


def _emp(db_session, name="Vikram", *, column_property="SJCES"):
    emp = make_employee(db_session, place_primary=False, property_id=column_property,
                        full_name=name, pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    return emp


# --- CRITICAL: double payment ------------------------------------------------

def test_two_open_active_primaries_are_impossible_at_the_database(db_session):
    """The invariant was asserted in a docstring and enforced nowhere. A partial
    unique index now makes the state unreachable."""
    from sqlalchemy.exc import IntegrityError

    _seed(db_session)
    emp = _emp(db_session)
    db_session.add_all([
        EmployeeAssignment(employee_id=emp.employee_id, property_id="SJCES",
                           is_primary=True, status="active", effective_from=_ANCHOR),
        EmployeeAssignment(employee_id=emp.employee_id, property_id="58033",
                           is_primary=True, status="active", effective_from=_ANCHOR),
    ])
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_the_pay_run_population_refuses_overlapping_primaries(db_session):
    """Belt and braces. The index cannot catch a primary with a far-future
    effective_to overlapping an open one, so the read side raises BEFORE any
    provider call. Review reproduced $500 submitted where $250 was owed."""
    _seed(db_session)
    emp = _emp(db_session)
    db_session.add_all([
        EmployeeAssignment(employee_id=emp.employee_id, property_id="SJCES",
                           is_primary=True, status="active", effective_from=_ANCHOR,
                           effective_to=date(2030, 1, 1)),
        EmployeeAssignment(employee_id=emp.employee_id, property_id="58033",
                           is_primary=True, status="active", effective_from=_ANCHOR),
    ])
    db_session.commit()

    with pytest.raises(AmbiguousPrimaryError):
        employee_ids_with_primary_at(db_session, "SJCES", _LATER)


def test_a_transfer_is_still_allowed(db_session):
    """The first version of the index forbade the ordinary transfer pattern.
    Closing the old primary and opening the new one must remain legal."""
    _seed(db_session)
    emp = _emp(db_session)
    db_session.add_all([
        EmployeeAssignment(employee_id=emp.employee_id, property_id="SJCES",
                           is_primary=True, status="active", effective_from=_ANCHOR,
                           effective_to=date(2026, 3, 1)),
        EmployeeAssignment(employee_id=emp.employee_id, property_id="58033",
                           is_primary=True, status="active",
                           effective_from=date(2026, 3, 1)),
    ])
    db_session.commit()

    assert emp.employee_id in employee_ids_with_primary_at(db_session, "58033", _LATER)
    assert emp.employee_id not in employee_ids_with_primary_at(db_session, "SJCES", _LATER)


# --- CRITICAL: the ramp gate, applied as a class -----------------------------

def test_rows_that_are_not_effective_do_not_fall_back_to_the_column(db_session):
    """THE class-level fix. 'No rows effective now' is not 'no rows at all' --
    the assignments have a definite opinion and the retiring column must not
    overrule it. This shipped once as a kiosk readmitting leavers, and review
    then found it in five more ramps."""
    _seed(db_session)
    emp = _emp(db_session, column_property="58033")
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
        status="active", effective_from=_ANCHOR, effective_to=date(2026, 3, 1),
    ))
    db_session.commit()

    assert has_assignment_rows(db_session, emp.employee_id) is True
    # Ended assignment, and the column says 58033 -- neither may confer anything.
    assert emp.employee_id not in employee_ids_with_primary_at(db_session, "58033", _LATER)
    assert emp.employee_id not in employee_ids_with_primary_at(db_session, "SJCES", _LATER)


def test_exemption_comes_from_the_assignments_position(db_session):
    """Exempt zeroes labor cost and drops the person out of the priced
    population every suppression gate counts, so where exemption is READ from is
    a money question. Only the assignment in effect may answer it.

    This test used to prove the retiring `Employee.position_id` could not
    override the assignment. Task 9 deleted that column, so the override is now
    unrepresentable; what survives is the positive claim.
    """
    dept = _seed(db_session)
    exempt_pos = Position(department_id=dept.department_id, title="GM", flsa_exempt=True)
    hourly_pos = Position(department_id=dept.department_id, title="FD", flsa_exempt=False)
    db_session.add_all([exempt_pos, hourly_pos])
    db_session.flush()

    emp = _emp(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        position_id=hourly_pos.position_id, is_primary=True, status="active",
        effective_from=_ANCHOR,
    ))
    db_session.commit()

    assert is_exempt_on(db_session, emp.employee_id, _LATER) is False


def test_a_null_position_is_not_exemption(db_session):
    """Silence is not exemption. An assignment carrying no position means we have
    not been told, and the protective answer is non-exempt."""
    dept = _seed(db_session)
    exempt_pos = Position(department_id=dept.department_id, title="GM", flsa_exempt=True)
    db_session.add(exempt_pos)
    db_session.flush()

    emp = _emp(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES", position_id=None,
        is_primary=True, status="active", effective_from=_ANCHOR,
    ))
    db_session.commit()

    assert is_exempt_on(db_session, emp.employee_id, _LATER) is False


# --- CRITICAL/HIGH: termination must close the assignments -------------------

def test_terminate_closes_every_active_assignment(db_session):
    """The model rests on 'effective-dating alone answers whether this person was
    employed here'. The runtime path set only termination_date, so terminated
    staff stayed live in every population -- and Task 9 removes the column that
    accidentally masked it."""
    _seed(db_session)
    emp = _emp(db_session)
    db_session.add_all([
        EmployeeAssignment(employee_id=emp.employee_id, property_id="SJCES",
                           is_primary=True, status="active", effective_from=_ANCHOR),
        EmployeeAssignment(employee_id=emp.employee_id, property_id="58033",
                           is_primary=False, status="active", effective_from=_ANCHOR),
    ])
    db_session.commit()

    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp.employee_id,
                       actor_subject="admin", on_date=date(2026, 2, 10))
    db_session.commit()

    rows = db_session.execute(
        select(EmployeeAssignment).where(EmployeeAssignment.employee_id == emp.employee_id)
    ).scalars().all()
    assert all(a.status == "inactive" for a in rows)
    assert all(a.effective_to == date(2026, 2, 11) for a in rows), (
        "effective_to is EXCLUSIVE, so it closes the day AFTER the last day "
        "worked -- otherwise the final day drops out of the pay run that owes it"
    )
    assert emp.employee_id not in employee_ids_with_primary_at(db_session, "SJCES", _LATER)


def test_the_last_day_worked_is_still_inside_the_assignment(db_session):
    """The off-by-one that would silently drop a terminated employee's final
    day from the pay run that has to pay for it."""
    _seed(db_session)
    emp = _emp(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
        status="active", effective_from=_ANCHOR,
    ))
    db_session.commit()

    last_day = date(2026, 2, 10)
    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp.employee_id,
                       actor_subject="admin", on_date=last_day)
    db_session.commit()

    # Status is inactive so the person is gone from active populations, but the
    # dating still covers their final worked day for anything reading history.
    assignment = db_session.execute(select(EmployeeAssignment)).scalar_one()
    assert assignment.effective_from <= last_day < assignment.effective_to


# --- the jurisdiction default -------------------------------------------------

def test_a_new_property_must_declare_its_wage_jurisdiction(db_session):
    """A lingering 'US-CA' server default silently gave a new out-of-state
    property California daily overtime and double time. overtime_rules refuses an
    UNRECOGNIZED jurisdiction, never an unset one."""
    from sqlalchemy import text

    _seed(db_session)
    default = db_session.execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'property' AND column_name = 'wage_jurisdiction'"
    )).scalar_one_or_none()
    assert default is None, (
        "wage_jurisdiction must have no server default -- an unset property "
        "should be an explicit decision, not silently Californian"
    )


def test_timecards_are_controlled_by_the_primary_assignments_property(db_session):
    """The SEVENTH authorization site, which the six-site cutover missed. It
    gates read/approve/adjust, so the controlling GM was whichever property the
    retiring column named."""
    from usali.models import Timecard
    from usali.timecard_api import _card_property

    _seed(db_session)
    emp = _emp(db_session, column_property="58033")  # column disagrees
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
        status="active", effective_from=_ANCHOR,
    ))
    db_session.flush()
    card = Timecard(employee_id=emp.employee_id, period_start=_ANCHOR,
                    period_end=_ANCHOR + timedelta(days=13), status="open")
    db_session.add(card)
    db_session.commit()

    assert _card_property(db_session, card) == "SJCES"
