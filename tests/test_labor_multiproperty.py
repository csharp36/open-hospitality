"""Labor promotion across two properties.

The load-bearing property here is ORDER: overtime runs on the employee's
combined hours FIRST, and only the resulting hours are split across properties.
Splitting first and running overtime per property turns 6h at one hotel plus 5h
at the other into two sub-8-hour days with zero daily overtime -- an
underpayment, not a reporting error.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.employees import make_employee, set_rate_everywhere
from usali.labor import promote_timecard
from usali.models import (
    Department,
    EmployeeAssignment,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
    Timecard,
    UsaliLaborFact,
)
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_DAY = date(2026, 1, 6)


def _at(hour, minute=0):
    return datetime(2026, 1, 6, hour, minute, tzinfo=timezone.utc)


def _seed(db_session, *, rate="20.00", sjces_jurisdiction="US-CA"):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE",
                            pms_source="OPERA", wage_jurisdiction=sjces_jurisdiction))
    db_session.add(Property(property_id="58033", org_id=1, name="BW",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    front = Department(property_id="SJCES", name="Front Office")
    laundry = Department(property_id="58033", name="Laundry")
    db_session.add_all([front, laundry])
    db_session.flush()
    pos = Position(department_id=front.department_id, title="FRONT DESK", flsa_exempt=False)
    db_session.add(pos)
    sjces_kiosk = KioskDevice(property_id="SJCES", name="a", token_hash="a" * 64,
                              enrolled_by="admin")
    bw_kiosk = KioskDevice(property_id="58033", name="b", token_hash="b" * 64,
                           enrolled_by="admin")
    emp = make_employee(db_session, place_primary=False, property_id="SJCES", full_name="Vikram Jindal",
                   pay_type="hourly")
    db_session.add_all([sjces_kiosk, bw_kiosk, emp])
    db_session.flush()
    db_session.add_all([
        EmployeeAssignment(employee_id=emp.employee_id, property_id="SJCES",
                           department_id=front.department_id, position_id=pos.position_id,
                           is_primary=True, status="active", effective_from=_ANCHOR),
        EmployeeAssignment(employee_id=emp.employee_id, property_id="58033",
                           department_id=laundry.department_id,
                           is_primary=False, status="active", effective_from=_ANCHOR),
    ])
    db_session.flush()
    # Since E2 the rate hangs off the PLACEMENT, so both of Vikram's placements
    # need one. These tests are about splitting hours and cost across two
    # properties at ONE rate; per-placement rates are exercised separately.
    set_rate_everywhere(db_session, emp, rate)
    card = Timecard(employee_id=emp.employee_id, period_start=_ANCHOR,
                    period_end=_ANCHOR + timedelta(days=13), status="approved")
    db_session.add(card)
    db_session.flush()
    return emp, card, sjces_kiosk, bw_kiosk, front, laundry


def _span(db_session, card, emp, device, start, end):
    db_session.add_all([
        Punch(employee_id=emp.employee_id, kiosk_device_id=device.device_id,
              punch_type="clock_in", punched_at=_at(start), business_date=_DAY,
              timecard_id=card.timecard_id),
        Punch(employee_id=emp.employee_id, kiosk_device_id=device.device_id,
              punch_type="clock_out", punched_at=_at(end), business_date=_DAY,
              timecard_id=card.timecard_id),
    ])


def _facts(db_session):
    return {
        f.property_id: f
        for f in db_session.execute(select(UsaliLaborFact)).scalars()
    }


def test_one_timecard_spanning_two_properties_emits_a_fact_per_property(db_session):
    emp, card, sjces, bw, _front, _laundry = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)   # 6h SJCES
    _span(db_session, card, emp, bw, 13, 18)     # 5h 58033
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = _facts(db_session)
    assert set(facts) == {"SJCES", "58033"}
    assert facts["SJCES"].hours == Decimal("6.00")
    assert facts["58033"].hours == Decimal("5.00")


def test_overtime_is_computed_on_combined_hours_not_per_property(db_session):
    """THE legal invariant. 6h + 5h on one day is 11 hours worked and 3 hours of
    California daily overtime. Computing per property yields zero and underpays.

    Cal. Labor Code 500 defines a workday temporally with no work-site
    qualifier; 510(a) applies the 8/12 thresholds to 'one workday'; Wage Order 5
    scopes 'employer' and 'hours worked' to the employer, not the site.
    """
    emp, card, sjces, bw, _front, _laundry = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)
    _span(db_session, card, emp, bw, 13, 18)
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = _facts(db_session)
    total_premium = sum(f.ot_hours for f in facts.values())
    assert total_premium == Decimal("3.00"), (
        "11 hours in one workday owes 3 hours of daily OT; per-property "
        "computation would find none"
    )
    assert sum(f.hours for f in facts.values()) == Decimal("11.00")


def test_the_overtime_premium_is_shared_by_both_properties(db_session):
    """The 3 OT hours arose from the combined day, so both hotels carry a share
    proportional to the hours they contributed -- 6:5. Charging the whole
    premium to whichever hotel was worked LAST would be arbitrary."""
    emp, card, sjces, bw, _front, _laundry = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)
    _span(db_session, card, emp, bw, 13, 18)
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = _facts(db_session)
    assert facts["SJCES"].ot_hours > 0 and facts["58033"].ot_hours > 0
    assert facts["SJCES"].ot_hours + facts["58033"].ot_hours == Decimal("3.00")


def test_cost_is_split_and_sums_to_the_single_employer_total(db_session):
    """8 regular + 3 OT at $20 = 160 + 90 = $250 across both hotels."""
    emp, card, sjces, bw, _front, _laundry = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)
    _span(db_session, card, emp, bw, 13, 18)
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = _facts(db_session)
    assert sum(f.est_cost for f in facts.values()) == Decimal("250.00")


def test_each_fact_carries_the_department_of_that_propertys_assignment(db_session):
    """The same person is Front Office at one hotel and Laundry at the other.
    A single employee-wide department would mis-file half their hours."""
    emp, card, sjces, bw, front, laundry = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)
    _span(db_session, card, emp, bw, 13, 18)
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = _facts(db_session)
    assert facts["SJCES"].department_id == front.department_id
    assert facts["58033"].department_id == laundry.department_id


def test_single_property_card_is_unchanged(db_session):
    """The overwhelmingly common case must behave exactly as before E1."""
    emp, card, sjces, _bw, front, _laundry = _seed(db_session)
    _span(db_session, card, emp, sjces, 9, 17)
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = _facts(db_session)
    assert set(facts) == {"SJCES"}
    assert facts["SJCES"].hours == Decimal("8.00")
    assert facts["SJCES"].ot_hours == Decimal("0.00")
    assert facts["SJCES"].est_cost == Decimal("160.00")
    assert facts["SJCES"].department_id == front.department_id


def test_a_card_spanning_two_wage_jurisdictions_is_refused(db_session):
    """Overtime runs on combined hours, so one ruleset governs the card. Which
    state's daily rule applies to a day split across state lines is a genuine
    legal question with no answer encoded here -- refuse rather than pick."""
    emp, card, sjces, bw, _front, _laundry = _seed(db_session, sjces_jurisdiction="US")
    _span(db_session, card, emp, sjces, 6, 12)
    _span(db_session, card, emp, bw, 13, 18)
    db_session.commit()

    with pytest.raises(ValueError, match="multiple wage"):
        promote_timecard(db_session, card, anchor=_ANCHOR)


def test_hours_split_never_loses_or_invents_time(db_session):
    """Uneven spans exercise largest-remainder rounding. A Schedule 14 that does
    not tie is the failure mode."""
    emp, card, sjces, bw, _front, _laundry = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 9)        # 3h
    db_session.add_all([
        Punch(employee_id=emp.employee_id, kiosk_device_id=bw.device_id,
              punch_type="clock_in", punched_at=_at(10), business_date=_DAY,
              timecard_id=card.timecard_id),
        Punch(employee_id=emp.employee_id, kiosk_device_id=bw.device_id,
              punch_type="clock_out", punched_at=_at(16, 20), business_date=_DAY,
              timecard_id=card.timecard_id),
    ])
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = _facts(db_session)
    # 3h + 6h20m = 9h20m worked; over 8 gives 1h20m of daily OT.
    assert sum(f.hours for f in facts.values()) == Decimal("9.33")
    assert sum(f.ot_hours for f in facts.values()) == Decimal("1.33")
