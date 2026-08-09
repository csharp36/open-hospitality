"""EmployeeAssignment: the model that lets one person work at two properties.

21 of 28 real staff at the pilot hold assignments at both hotels, and three hold
two DIFFERENT jobs. `Employee.property_id` / `department_id` / `position_id` --
single FKs -- cannot express either, which is why every hour those people work
is currently attributed to whichever one property their employee row names.
"""

from datetime import date

import pytest
from sqlalchemy import select

from tests.employees import make_employee
from usali.assignments import AmbiguousAssignmentError, assignment_at
from usali.models import (
    Department,
    EmployeeAssignment,
    Organization,
    Position,
    Property,
)
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE SJC", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="58033", org_id=1, name="BW SureStay", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    front = Department(property_id="SJCES", name="Front Office")
    db_session.add(front)
    db_session.flush()
    night_auditor = Position(
        department_id=front.department_id, title="NIGHT AUDITOR", flsa_exempt=False
    )
    db_session.add(night_auditor)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES", full_name="Vikram Jindal", pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    return emp, front, night_auditor


def test_employee_may_hold_assignments_at_two_properties(db_session):
    emp, front, pos = _seed(db_session)
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES",
            department_id=front.department_id, position_id=pos.position_id,
            is_primary=True, status="active", effective_from=date(2026, 1, 5),
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="58033",
            department_id=None, position_id=None,
            is_primary=False, status="active", effective_from=date(2026, 1, 5),
        ),
    ])
    db_session.commit()

    rows = db_session.execute(
        select(EmployeeAssignment).where(EmployeeAssignment.employee_id == emp.employee_id)
    ).scalars().all()
    assert {r.property_id for r in rows} == {"SJCES", "58033"}
    assert sum(1 for r in rows if r.is_primary) == 1, "exactly one assignment issues the paycheck"


def test_same_person_may_hold_two_different_positions(db_session):
    """Vikram is a FRONT DESK ASSOCIATE at one hotel and a NIGHT AUDITOR at the
    other -- jobs that carry different rates. A single Employee.position_id
    cannot represent this, and pricing his hours at one rate is simply wrong."""
    emp, front, night_auditor = _seed(db_session)
    fda = Position(department_id=front.department_id, title="FRONT DESK ASSOCIATE", flsa_exempt=False)
    db_session.add(fda)
    db_session.flush()
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES",
            department_id=front.department_id, position_id=night_auditor.position_id,
            is_primary=True, status="active", effective_from=date(2026, 1, 5),
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="58033",
            department_id=front.department_id, position_id=fda.position_id,
            is_primary=False, status="active", effective_from=date(2026, 1, 5),
        ),
    ])
    db_session.commit()

    titles = {
        db_session.get(Position, a.position_id).title
        for a in db_session.execute(select(EmployeeAssignment)).scalars()
    }
    assert titles == {"NIGHT AUDITOR", "FRONT DESK ASSOCIATE"}


# --- effective-dated resolution ---------------------------------------------

def test_dating_is_inclusive_from_exclusive_to(db_session):
    """A transfer dated 2026-03-01 gives that day ENTIRELY to the new assignment
    and none of it to the old. An off-by-one here mis-attributes a full day of
    labor cost to the wrong property, and nothing downstream would signal it."""
    from usali.assignments import assignments_on

    emp, _, _ = _seed(db_session)
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
            status="active", effective_from=date(2026, 1, 5), effective_to=date(2026, 3, 1),
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="58033", is_primary=True,
            status="active", effective_from=date(2026, 3, 1), effective_to=None,
        ),
    ])
    db_session.commit()

    def props(on):
        return {a.property_id for a in assignments_on(db_session, emp.employee_id, on)}

    assert props(date(2026, 2, 28)) == {"SJCES"}, "day before transfer belongs to the old property"
    assert props(date(2026, 3, 1)) == {"58033"}, "transfer day belongs to the NEW property"
    assert props(date(2026, 3, 2)) == {"58033"}


def test_assignment_is_not_yet_effective_before_its_start(db_session):
    from usali.assignments import assignments_on

    emp, _, _ = _seed(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
        status="active", effective_from=date(2026, 6, 1),
    ))
    db_session.commit()

    assert assignments_on(db_session, emp.employee_id, date(2026, 5, 31)) == []
    assert len(assignments_on(db_session, emp.employee_id, date(2026, 6, 1))) == 1


def test_inactive_assignments_are_excluded_unless_asked_for(db_session):
    """A terminated person still has a row; costing must not pick it up, but an
    audit view may need to see it."""
    from usali.assignments import assignments_on

    emp, _, _ = _seed(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
        status="inactive", effective_from=date(2026, 1, 5),
    ))
    db_session.commit()

    on = date(2026, 4, 1)
    assert assignments_on(db_session, emp.employee_id, on) == []
    assert len(assignments_on(db_session, emp.employee_id, on, active_only=False)) == 1


def test_primary_assignment_is_the_paycheck_issuing_one(db_session):
    from usali.assignments import primary_assignment_on

    emp, _, _ = _seed(db_session)
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
            status="active", effective_from=date(2026, 1, 5),
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="58033", is_primary=False,
            status="active", effective_from=date(2026, 1, 5),
        ),
    ])
    db_session.commit()

    primary = primary_assignment_on(db_session, emp.employee_id, date(2026, 4, 1))
    assert primary is not None and primary.property_id == "SJCES"


def test_primary_assignment_is_none_when_nothing_is_effective(db_session):
    """Callers must handle this: a pay run cannot silently pick an arbitrary
    property for someone with no effective assignment."""
    from usali.assignments import primary_assignment_on

    emp, _, _ = _seed(db_session)
    db_session.commit()
    assert primary_assignment_on(db_session, emp.employee_id, date(2026, 4, 1)) is None


def test_two_primaries_on_one_date_is_reported_not_silently_resolved(db_session):
    """Data corruption that MUST NOT resolve to a coin flip -- two primaries is
    exactly what would let one timecard reach two pay runs and pay twice
    (reproduced by review: $500 submitted where $250 was owed).

    A partial unique index now forbids two simultaneously OPEN active primaries.
    This constructs the residue it cannot catch -- one primary carrying a
    far-future effective_to overlapping an open one -- which is exactly why the
    read-side check has to exist as well."""
    from usali.assignments import AmbiguousPrimaryError, primary_assignment_on

    emp, _, _ = _seed(db_session)
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
            status="active", effective_from=date(2026, 1, 5),
            effective_to=date(2030, 1, 1),  # open enough to overlap, not NULL
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="58033", is_primary=True,
            status="active", effective_from=date(2026, 1, 5),
        ),
    ])
    db_session.commit()

    with pytest.raises(AmbiguousPrimaryError, match="two pay runs"):
        primary_assignment_on(db_session, emp.employee_id, date(2026, 4, 1))


def test_property_ids_on_returns_every_effective_property(db_session):
    from usali.assignments import property_ids_on

    emp, _, _ = _seed(db_session)
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
            status="active", effective_from=date(2026, 1, 5),
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="58033", is_primary=False,
            status="active", effective_from=date(2026, 1, 5),
        ),
    ])
    db_session.commit()
    assert property_ids_on(db_session, emp.employee_id, date(2026, 4, 1)) == {"SJCES", "58033"}


def test_assignment_at_finds_the_placement_at_that_property(db_session):
    """Rates and departments both hang off a PLACEMENT, so both need the same
    lookup. One resolver, so they cannot disagree about which one it is."""
    emp, front, pos = _seed(db_session)
    sjces = EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        department_id=front.department_id, position_id=pos.position_id,
        is_primary=True, status="active", effective_from=date(2026, 1, 5),
    )
    other = EmployeeAssignment(
        employee_id=emp.employee_id, property_id="58033",
        department_id=None, position_id=None,
        is_primary=False, status="active", effective_from=date(2026, 1, 5),
    )
    db_session.add_all([sjces, other])
    db_session.flush()

    found = assignment_at(db_session, emp.employee_id, "58033", date(2026, 4, 15))
    assert found is not None
    assert found.assignment_id == other.assignment_id


def test_assignment_at_returns_none_where_the_person_does_not_work(db_session):
    """Reachable, not exceptional: attribution is kiosk-derived and therefore
    WIDER than assignment, so a punch can land at a property someone has since
    transferred away from. The hours are real and must not be dropped."""
    emp, front, pos = _seed(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        department_id=front.department_id, position_id=pos.position_id,
        is_primary=True, status="active", effective_from=date(2026, 1, 5),
    ))
    db_session.flush()

    assert assignment_at(db_session, emp.employee_id, "58033", date(2026, 4, 15)) is None


def test_two_placements_at_one_property_are_refused_not_picked(db_session):
    """Since E2 this choice selects a RATE, so picking one arbitrarily prices
    real worked hours at a figure nobody chose — the same refusal as
    AmbiguousRateError. Before E2 it only chose a department and quietly took
    the first.

    The unique constraint on (employee, property, effective_from) narrows this
    but does not close it: two placements STARTING on different dates and both
    still open overlap on every date after the later start. Picking up a second
    job in Laundry without closing the Front Office one is exactly that shape.
    """
    emp, front, pos = _seed(db_session)
    laundry = Department(property_id="SJCES", name="Laundry")
    db_session.add(laundry)
    db_session.flush()
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES",
            department_id=front.department_id, position_id=pos.position_id,
            is_primary=True, status="active", effective_from=date(2026, 1, 5),
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES",
            department_id=laundry.department_id, position_id=None,
            is_primary=False, status="active", effective_from=date(2026, 3, 1),
        ),
    ])
    db_session.flush()

    with pytest.raises(AmbiguousAssignmentError):
        assignment_at(db_session, emp.employee_id, "SJCES", date(2026, 4, 15))
