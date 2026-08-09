"""A closed period must cost the same no matter WHEN it is computed.

This is the D3 invariant generalised. D3 said cost must not vary with a
caller-supplied `as_of`; this says it must not vary with the *computation date*
either. A raise entered today must not move last quarter's Schedule 14.

It is the one failure in E2 that cannot be caught by reading a diff. The
arithmetic succeeds, the number changes, and a Schedule 14 that was already
filed no longer ties to what was filed. Nothing signals it.

Written BEFORE `assignment_rate` existed, deliberately. `Employee.pay_rate` was
a scalar with no dating, so re-promoting after a raise re-costed already-worked
hours at the new rate -- these tests FAILED, and that failure WAS the
specification for E2. They carried `xfail(strict=True)` so the suite would break
the moment they started passing; E2 Task 4 landed the cutover and the markers
came off.

Only the `_world` and `_give_a_raise` helpers changed. Not one line of the test
bodies moved, which is the whole return on writing them first: they were written
against no implementation, so they cannot have been shaped to fit one.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee, place
from usali.labor import promote_timecard
from usali.models import (
    AssignmentRate,
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
_CLOSED_START = date(2026, 4, 6)      # a period long since filed
_CLOSED_END = date(2026, 4, 19)
_WORKED = date(2026, 4, 7)
_RAISE_DATE = date(2026, 8, 1)        # months AFTER the closed period


def _world(db_session, *, rate="20.00"):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="SJCES", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="FRONT DESK",
                   flsa_exempt=False)
    device = KioskDevice(property_id="SJCES", name="iPad", token_hash="a" * 64,
                         enrolled_by="adm")
    db_session.add_all([pos, device])
    db_session.flush()

    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Hank H", pay_type="hourly")
    db_session.flush()
    assignment = place(db_session, emp, property_id="SJCES",
                       department_id=dept.department_id,
                       position_id=pos.position_id, is_primary=True,
                       effective_from=_ANCHOR)
    db_session.add(AssignmentRate(assignment_id=assignment.assignment_id,
                                  rate_type="regular", amount=rate,
                                  effective_from=_ANCHOR))
    db_session.flush()

    card = Timecard(employee_id=emp.employee_id, period_start=_CLOSED_START,
                    period_end=_CLOSED_END, status="approved")
    db_session.add(card)
    db_session.flush()
    for ptype, hour in (("clock_in", 9), ("clock_out", 17)):   # 8h
        db_session.add(Punch(
            employee_id=emp.employee_id, kiosk_device_id=device.device_id,
            punch_type=ptype,
            punched_at=datetime(_WORKED.year, _WORKED.month, _WORKED.day, hour,
                                tzinfo=UTC),
            business_date=_WORKED, timecard_id=card.timecard_id,
        ))
    db_session.commit()
    return emp, card


def _facts(db_session, card):
    """Every field that lands on a Schedule 14, as comparable tuples.

    Compares the WHOLE row, not just cost: a rate change could also move which
    department or business date the hours file under, and a test watching only
    `est_cost` would call that stable.
    """
    return sorted(
        (
            f.property_id, f.business_date, f.department_id,
            Decimal(str(f.hours)), Decimal(str(f.ot_hours)),
            Decimal(str(f.est_cost)),
        )
        for f in db_session.execute(
            select(UsaliLaborFact).where(UsaliLaborFact.timecard_id == card.timecard_id)
        ).scalars()
    )


def _give_a_raise(db_session, emp, new_rate):
    """THE ORDINARY RAISE: close the open rate, open the next one.

    This is the shape the partial unique index deliberately permits, and it is
    why the index is scoped `WHERE effective_to IS NULL` rather than forbidding
    two rows outright.

    Before E2 this overwrote a scalar, because there was nowhere to put an
    effective date. The test BODIES below have not changed a line -- only this
    helper -- which is exactly what writing them first was for.
    """
    open_rate = db_session.execute(
        select(AssignmentRate)
        .join(EmployeeAssignment,
              EmployeeAssignment.assignment_id == AssignmentRate.assignment_id)
        .where(EmployeeAssignment.employee_id == emp.employee_id,
               AssignmentRate.rate_type == "regular",
               AssignmentRate.effective_to.is_(None))
    ).scalar_one()
    open_rate.effective_to = _RAISE_DATE
    db_session.add(AssignmentRate(assignment_id=open_rate.assignment_id,
                                  rate_type="regular", amount=new_rate,
                                  effective_from=_RAISE_DATE))
    db_session.flush()


def test_repromoting_a_closed_period_reproduces_it_exactly(db_session):
    """The headline. Promote a closed period, grant a raise dated MONTHS later,
    re-promote, and the period must be byte-identical.

    Re-promotion is not hypothetical: `promote_timecard` is idempotent by
    design, the CLI exposes a backfill, and a correction to any single card
    re-runs it. Every one of those paths currently re-prices history.
    """
    emp, card = _world(db_session, rate="20.00")
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    before = _facts(db_session, card)
    assert before, "the fixture must actually promote something"

    _give_a_raise(db_session, emp, "30.00")
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    assert _facts(db_session, card) == before, (
        "a raise dated after the period moved the period's cost -- a filed "
        "Schedule 14 no longer ties to what was filed"
    )


def test_the_closed_period_keeps_the_rate_that_was_in_force(db_session):
    """Not merely 'stable' -- stable at the RIGHT number.

    A test asserting only that two computations agree would be satisfied by both
    being wrong in the same way. 8h at $20 is $160, and it stays $160 after the
    raise. Pinning the value is what makes this a fence rather than a tautology.
    """
    emp, card = _world(db_session, rate="20.00")
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert sum(f[5] for f in _facts(db_session, card)) == Decimal("160.0000")

    _give_a_raise(db_session, emp, "30.00")
    db_session.commit()
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    assert sum(f[5] for f in _facts(db_session, card)) == Decimal("160.0000"), (
        "8h at the $20 in force during the period; the $30 raise starts "
        f"{_RAISE_DATE.isoformat()} and owns no hour of it"
    )


def test_an_open_period_does_pick_up_a_raise(db_session):
    """The complement, which never needed a marker -- it held before E2 and
    must hold after.

    The fix must not become "cost never changes". Hours worked ON or after the
    raise date are owed the new rate; only hours already worked are frozen.
    Without this, freezing everything would satisfy the two tests above.
    """
    emp, card = _world(db_session, rate="20.00")
    _give_a_raise(db_session, emp, "30.00")
    db_session.commit()

    # A card whose worked day falls AFTER the raise date.
    later = Timecard(employee_id=emp.employee_id,
                     period_start=_RAISE_DATE,
                     period_end=_RAISE_DATE + timedelta(days=13),
                     status="approved")
    db_session.add(later)
    db_session.flush()
    device = db_session.execute(select(KioskDevice)).scalars().first()
    assert device is not None
    for ptype, hour in (("clock_in", 9), ("clock_out", 17)):
        db_session.add(Punch(
            employee_id=emp.employee_id, kiosk_device_id=device.device_id,
            punch_type=ptype,
            punched_at=datetime(_RAISE_DATE.year, _RAISE_DATE.month,
                                _RAISE_DATE.day, hour, tzinfo=UTC),
            business_date=_RAISE_DATE, timecard_id=later.timecard_id,
        ))
    db_session.commit()

    promote_timecard(db_session, later, anchor=_ANCHOR)
    db_session.commit()

    assert sum(f[5] for f in _facts(db_session, later)) == Decimal("240.0000"), (
        "8h worked on the raise date is owed the NEW rate; freezing it would be "
        "an underpayment, not stability"
    )
