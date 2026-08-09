"""Authorization under multi-property assignments.

Task 7 WIDENS access deliberately: someone assigned to both hotels must be able
to punch at either kiosk. The risk is widening too far -- letting a device reach
an employee who does not work there. Both directions are tested.

The authorization question ("works here") is deliberately WIDER than the payment
question ("paid by here"). Using the primary assignment for authorization would
lock people out of the second hotel they actually work at.
"""

from datetime import date, timedelta

import pytest

from tests.employees import make_employee
from usali.assignments import (
    employee_ids_serving_property,
    employee_ids_with_primary_at,
    employee_serves_property,
)
from usali.models import (
    Department,
    EmployeeAssignment,
    Organization,
    Property,
)
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_TODAY = date(2026, 4, 1)


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="58033", org_id=1, name="BW", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="OTHER", org_id=1, name="Not ours", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="SJCES", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    return dept


def _emp(db_session, name, placements, dept):
    """placements: list of (property_id, is_primary)."""
    emp = make_employee(db_session, place_primary=False, property_id=placements[0][0], full_name=name, pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    for property_id, primary in placements:
        db_session.add(EmployeeAssignment(
            employee_id=emp.employee_id, property_id=property_id,
            department_id=dept.department_id if property_id == "SJCES" else None,
            is_primary=primary, status="active", effective_from=_ANCHOR,
        ))
    db_session.flush()
    return emp


def test_a_dual_property_employee_is_admitted_at_both_kiosks(db_session):
    """The widening that Task 7 exists for. Vikram's paycheck comes from SJCES,
    but he must be able to punch at the 58033 kiosk."""
    dept = _seed(db_session)
    vikram = _emp(db_session, "Vikram", [("SJCES", True), ("58033", False)], dept)
    db_session.commit()

    assert employee_serves_property(db_session, vikram.employee_id, "SJCES", _TODAY)
    assert employee_serves_property(db_session, vikram.employee_id, "58033", _TODAY)


def test_authorization_is_wider_than_payment(db_session):
    """Explicitly: 58033 may not PAY Vikram, but must ADMIT him."""
    dept = _seed(db_session)
    vikram = _emp(db_session, "Vikram", [("SJCES", True), ("58033", False)], dept)
    db_session.commit()

    assert vikram.employee_id not in employee_ids_with_primary_at(db_session, "58033", _TODAY)
    assert vikram.employee_id in employee_ids_serving_property(db_session, "58033", _TODAY)


def test_a_kiosk_still_refuses_an_employee_who_does_not_work_there(db_session):
    """The widening must not become 'any employee at any device'."""
    dept = _seed(db_session)
    solo = _emp(db_session, "SJCES only", [("SJCES", True)], dept)
    db_session.commit()

    assert employee_serves_property(db_session, solo.employee_id, "SJCES", _TODAY)
    assert not employee_serves_property(db_session, solo.employee_id, "58033", _TODAY)
    assert not employee_serves_property(db_session, solo.employee_id, "OTHER", _TODAY)


def test_an_ended_assignment_stops_admitting(db_session):
    """Someone who transferred away must stop being admitted at the old kiosk."""
    dept = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES", full_name="Mover", pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        department_id=dept.department_id, is_primary=True, status="active",
        effective_from=_ANCHOR, effective_to=date(2026, 3, 1),
    ))
    db_session.commit()

    assert employee_serves_property(db_session, emp.employee_id, "SJCES", date(2026, 2, 28))
    assert not employee_serves_property(db_session, emp.employee_id, "SJCES", date(2026, 3, 1))


def test_an_inactive_assignment_does_not_admit(db_session):
    dept = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES", full_name="Gone", pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        department_id=dept.department_id, is_primary=True, status="inactive",
        effective_from=_ANCHOR,
    ))
    db_session.commit()

    assert not employee_serves_property(db_session, emp.employee_id, "SJCES", _TODAY)
    assert emp.employee_id not in employee_ids_serving_property(db_session, "SJCES", _TODAY)


def test_the_ramp_cannot_widen_access_for_an_assigned_employee(db_session):
    """Adversarial: the legacy column says 58033 while the assignments say only
    SJCES. An employee WITH assignments must be answered entirely from them, or
    the retiring column becomes a back door into a property they left."""
    dept = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="58033", full_name="Reassigned", pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        department_id=dept.department_id, is_primary=True, status="active",
        effective_from=_ANCHOR,
    ))
    db_session.commit()

    assert employee_serves_property(db_session, emp.employee_id, "SJCES", _TODAY)
    assert not employee_serves_property(db_session, emp.employee_id, "58033", _TODAY), (
        "the retiring column must not grant access the assignments deny"
    )
    assert emp.employee_id not in employee_ids_serving_property(db_session, "58033", _TODAY)


def test_an_employee_with_no_assignments_is_admitted_nowhere(db_session):
    """Task 9 deleted the ramp that answered from `Employee.property_id` when the
    assignment table was silent. An unassigned row now opens no kiosk at all.

    Failing closed is the right direction for a door: the cost is an onboarding
    hiccup someone reports in a minute, versus a person clocking in at a hotel
    no record says they work at.
    """
    _seed(db_session)
    unassigned = make_employee(db_session, place_primary=False, property_id="SJCES",
                               full_name="Unassigned", pay_type="hourly")
    db_session.add(unassigned)
    db_session.commit()

    assert not employee_serves_property(db_session, unassigned.employee_id, "SJCES", _TODAY)
    assert not employee_serves_property(db_session, unassigned.employee_id, "58033", _TODAY)


@pytest.mark.parametrize("on", [_ANCHOR - timedelta(days=1), _ANCHOR, _TODAY])
def test_admission_respects_effective_dating(db_session, on):
    dept = _seed(db_session)
    emp = _emp(db_session, "Starter", [("SJCES", True)], dept)
    db_session.commit()

    expected = on >= _ANCHOR
    assert employee_serves_property(db_session, emp.employee_id, "SJCES", on) is expected


def test_serving_population_agrees_with_punch_auth_on_end_dated_inactive(
    db_session,
):
    """The two predicates must be ONE predicate (in_effect_on). An
    end-dated assignment whose status was ALSO flipped inactive is in force
    until its end date — that is the E3 stranded-paycheck lesson the punch
    authorization already honours. Before F8 the serving population
    re-derived the rule with an unconditional status filter, so such a
    worker could punch but was invisible to kiosk search/identify — no way
    to self-identify in face-first mode."""
    dept = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Dated Out", pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        department_id=dept.department_id, is_primary=True, status="inactive",
        effective_from=_ANCHOR, effective_to=_TODAY + timedelta(days=14),
    ))
    db_session.commit()

    assert employee_serves_property(db_session, emp.employee_id, "SJCES",
                                    _TODAY), "sanity: punch auth admits"
    assert emp.employee_id in employee_ids_serving_property(
        db_session, "SJCES", _TODAY
    ), "the serving population must agree with the punch authorization"
