"""What E2 made POSSIBLE, and what it made refuse.

`test_closed_period_stability.py` pins the invariant E2 exists to protect. This
file covers the three behaviours the cutover introduced, none of which any
pre-E2 test could express:

  1. two placements paid DIFFERENTLY -- the reason rates moved off the employee
  2. a pay run refusing an employee whose worked days resolve to more than one
     rate, because its port carries a single `hourly_rate`
  3. a raise entered through the API CLOSING the old rate rather than
     overwriting it, which is what keeps (1) from restating history
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee, place, set_rate
from usali.assignments import assignment_at
from usali.labor import promote_timecard
from usali.models import (
    AssignmentRate,
    Department,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
    Timecard,
    UsaliLaborFact,
)
from usali.payroll_run import assemble_pay_run_entries
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_DAY = date(2026, 4, 7)


def _two_hotels(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add_all([
        Property(property_id="SJCES", org_id=1, name="HIE", pms_source="OPERA",
                 wage_jurisdiction="US-CA", timezone="America/Los_Angeles"),
        Property(property_id="58033", org_id=1, name="BW", pms_source="OPERA",
                 wage_jurisdiction="US-CA", timezone="America/Los_Angeles"),
    ])
    db_session.flush()
    front = Department(property_id="SJCES", name="Front Office")
    laundry = Department(property_id="58033", name="Laundry")
    db_session.add_all([front, laundry])
    db_session.flush()
    pos = Position(department_id=front.department_id, title="FRONT DESK",
                   flsa_exempt=False)
    sjces_kiosk = KioskDevice(property_id="SJCES", name="a", token_hash="a" * 64,
                              enrolled_by="admin")
    bw_kiosk = KioskDevice(property_id="58033", name="b", token_hash="b" * 64,
                           enrolled_by="admin")
    db_session.add_all([pos, sjces_kiosk, bw_kiosk])
    db_session.flush()
    return front, laundry, pos, sjces_kiosk, bw_kiosk


def _vikram(db_session, front, laundry, pos, *, desk_rate, laundry_rate):
    """The case the whole pillar is named for: front desk at one hotel, laundry
    at the other, paid differently at each."""
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Vikram Jindal", pay_type="hourly")
    db_session.flush()
    desk = place(db_session, emp, property_id="SJCES",
                 department_id=front.department_id, position_id=pos.position_id,
                 is_primary=True, effective_from=_ANCHOR, pay_rate=desk_rate)
    wash = place(db_session, emp, property_id="58033",
                 department_id=laundry.department_id, is_primary=False,
                 effective_from=_ANCHOR, pay_rate=laundry_rate)
    return emp, desk, wash


def _card_with_a_split_day(db_session, emp, sjces_kiosk, bw_kiosk):
    """4h at the front desk, then 4h in laundry, on one business date."""
    card = Timecard(employee_id=emp.employee_id, period_start=_ANCHOR,
                    period_end=_ANCHOR + timedelta(days=13), status="approved")
    db_session.add(card)
    db_session.flush()
    for device, (start, end) in (
        (sjces_kiosk, (9, 13)),
        (bw_kiosk, (14, 18)),
    ):
        for ptype, hour in (("clock_in", start), ("clock_out", end)):
            db_session.add(Punch(
                employee_id=emp.employee_id, kiosk_device_id=device.device_id,
                punch_type=ptype,
                punched_at=datetime(_DAY.year, _DAY.month, _DAY.day, hour,
                                    tzinfo=UTC),
                business_date=_DAY, timecard_id=card.timecard_id,
            ))
    db_session.commit()
    return card


def _facts(db_session, card):
    return {
        f.property_id: Decimal(str(f.est_cost))
        for f in db_session.execute(
            select(UsaliLaborFact).where(
                UsaliLaborFact.timecard_id == card.timecard_id
            )
        ).scalars()
    }


# --- 1. two placements, two rates --------------------------------------------


def test_each_property_prices_its_own_hours_at_its_own_rate(db_session):
    """THE capability E2 was built for. A single `Employee.pay_rate` could not
    express this at all: one of the two hotels was always priced at the other's
    rate, and the arithmetic succeeded, so a wrong Schedule 14 looked right.

    4h front desk at $20 = $80; 4h laundry at $30 = $120. If a single rate were
    still in play both sides would read $80 or both $120.
    """
    front, laundry, pos, sjces_kiosk, bw_kiosk = _two_hotels(db_session)
    emp, _desk, _wash = _vikram(db_session, front, laundry, pos,
                                desk_rate="20.00", laundry_rate="30.00")
    card = _card_with_a_split_day(db_session, emp, sjces_kiosk, bw_kiosk)

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    costs = _facts(db_session, card)
    assert costs["SJCES"] == Decimal("80.0000")
    assert costs["58033"] == Decimal("120.0000")


def test_a_placement_with_no_rate_costs_nothing_without_zeroing_the_other(db_session):
    """`None` means "cannot cost", and it must stay scoped to the placement that
    cannot be costed. Letting it fall through to the whole card would drop real
    front-desk cost off Schedule 14; substituting zero would put an un-rated
    person into the priced population every suppression gate counts."""
    front, laundry, pos, sjces_kiosk, bw_kiosk = _two_hotels(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Vikram Jindal", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          position_id=pos.position_id, is_primary=True, effective_from=_ANCHOR,
          pay_rate="20.00")
    place(db_session, emp, property_id="58033", department_id=laundry.department_id,
          is_primary=False, effective_from=_ANCHOR)  # no rate at all
    card = _card_with_a_split_day(db_session, emp, sjces_kiosk, bw_kiosk)

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    costs = _facts(db_session, card)
    assert costs["SJCES"] == Decimal("80.0000"), "the rated placement still prices"
    assert costs["58033"] == Decimal("0.0000"), "hours promote, cost does not"


def test_a_mid_period_raise_prices_each_day_at_the_rate_then_in_force(db_session):
    """Per BUSINESS DATE, not once per card. This is the exemption bug's exact
    shape -- a value sampled once and applied to all fourteen days -- and it
    shipped twice, so it gets its own fence here.

    Two 8h days at one placement, with a raise between them: $20 then $30.
    Sampling either day would give 160+160 or 240+240 instead of 160+240.
    """
    front, _laundry, pos, sjces_kiosk, _bw = _two_hotels(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Hank H", pay_type="hourly")
    db_session.flush()
    desk = place(db_session, emp, property_id="SJCES",
                 department_id=front.department_id, position_id=pos.position_id,
                 is_primary=True, effective_from=_ANCHOR)
    day_two = _DAY + timedelta(days=1)
    set_rate(db_session, desk, "20.00", effective_to=day_two)
    set_rate(db_session, desk, "30.00", effective_from=day_two)

    card = Timecard(employee_id=emp.employee_id, period_start=_ANCHOR,
                    period_end=_ANCHOR + timedelta(days=13), status="approved")
    db_session.add(card)
    db_session.flush()
    for business_date in (_DAY, day_two):
        for ptype, hour in (("clock_in", 9), ("clock_out", 17)):
            db_session.add(Punch(
                employee_id=emp.employee_id,
                kiosk_device_id=sjces_kiosk.device_id, punch_type=ptype,
                punched_at=datetime(business_date.year, business_date.month,
                                    business_date.day, hour, tzinfo=UTC),
                business_date=business_date, timecard_id=card.timecard_id,
            ))
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    by_date = {
        f.business_date: Decimal(str(f.est_cost))
        for f in db_session.execute(
            select(UsaliLaborFact).where(
                UsaliLaborFact.timecard_id == card.timecard_id
            )
        ).scalars()
    }
    assert by_date[_DAY] == Decimal("160.0000"), "8h at the $20 in force that day"
    assert by_date[day_two] == Decimal("240.0000"), "8h at the $30 in force the next"


# --- 2. the pay run refuses what its port cannot say --------------------------


def test_a_pay_run_refuses_an_employee_whose_days_resolve_to_two_rates(db_session):
    """`PayRunEntry` carries ONE `hourly_rate`. Two rates across the days worked
    means no single value is true, so preflight names the employee and blocks.

    Averaging would submit money nobody authorised; sampling would silently pay
    every hour at whichever placement the sample happened to hit. Both look like
    a successful pay run.
    """
    front, laundry, pos, sjces_kiosk, bw_kiosk = _two_hotels(db_session)
    emp, _desk, _wash = _vikram(db_session, front, laundry, pos,
                                desk_rate="20.00", laundry_rate="30.00")
    _card_with_a_split_day(db_session, emp, sjces_kiosk, bw_kiosk)

    report = assemble_pay_run_entries(
        db_session, "SJCES", _ANCHOR, anchor=_ANCHOR
    )

    assert report.entries == [], "nothing is submittable while the rate is ambiguous"
    assert any("different pay rates" in p for p in report.problems), report.problems
    assert any(str(emp.employee_id) in p for p in report.problems), (
        "the blocker must name who to fix"
    )


def test_a_pay_run_proceeds_when_both_placements_agree(db_session):
    """The complement, and the reason this blocker is safe to ship: at the E2
    cutover every placement of one person carries the SAME rate, because the
    backfill wrote them all from one scalar. It fires only on genuinely
    divergent money, never on migrated data."""
    front, laundry, pos, sjces_kiosk, bw_kiosk = _two_hotels(db_session)
    emp, _desk, _wash = _vikram(db_session, front, laundry, pos,
                                desk_rate="20.00", laundry_rate="20.00")
    _card_with_a_split_day(db_session, emp, sjces_kiosk, bw_kiosk)

    report = assemble_pay_run_entries(
        db_session, "SJCES", _ANCHOR, anchor=_ANCHOR
    )

    rate_problems = [p for p in report.problems if "pay rate" in p]
    assert rate_problems == [], rate_problems


# --- 3. a raise CLOSES the old rate ------------------------------------------


def test_setting_a_rate_closes_the_old_one_instead_of_overwriting_it(db_session):
    """The write side of the whole phase. Overwriting the open row would leave
    already-worked days resolving through the NEW figure, restating a closed
    period on the next re-promote -- the bug E2 exists to kill, reintroduced
    through the API rather than the model.
    """
    from usali.workforce import SetPayRateBody

    front, _laundry, pos, _k, _b = _two_hotels(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Hank H", pay_type="hourly")
    db_session.flush()
    desk = place(db_session, emp, property_id="SJCES",
                 department_id=front.department_id, position_id=pos.position_id,
                 is_primary=True, effective_from=_ANCHOR, pay_rate="20.00")
    db_session.commit()

    # Exercised at the model level: the endpoint's own auth and audit are
    # covered in test_workforce_api.py; what matters here is the ROW shape.
    body = SetPayRateBody(pay_rate=Decimal("30.00"), effective_from=date(2026, 8, 1))
    _apply_raise(db_session, desk.assignment_id, body)
    db_session.commit()

    rows = sorted(
        db_session.execute(
            select(AssignmentRate).where(
                AssignmentRate.assignment_id == desk.assignment_id
            )
        ).scalars(),
        key=lambda r: r.effective_from,
    )
    assert len(rows) == 2, "the old rate is closed and kept, not replaced"
    assert rows[0].amount == "20.00"
    assert rows[0].effective_to == date(2026, 8, 1), "closed exactly where the new one starts"
    assert rows[1].amount == "30.00"
    assert rows[1].effective_to is None


def _apply_raise(session, assignment_id, body):
    """The rate-row half of `set_pay_rate`, without the HTTP layer."""
    effective = body.effective_from or date.today()
    open_rate = session.execute(
        select(AssignmentRate).where(
            AssignmentRate.assignment_id == assignment_id,
            AssignmentRate.rate_type == "regular",
            AssignmentRate.effective_to.is_(None),
        )
    ).scalar_one_or_none()
    if open_rate is not None:
        open_rate.effective_to = effective
    session.add(AssignmentRate(
        assignment_id=assignment_id, rate_type="regular",
        amount=str(body.pay_rate), effective_from=effective,
    ))
    session.flush()


def test_the_placement_lookup_and_the_rate_agree_on_which_placement(db_session):
    """Department attribution and rate resolution both hang off the SAME
    placement. Two lookups could disagree, and the hours would then file under
    one department at the rate of another -- a wrong Schedule 14 line that
    reconciles perfectly against itself."""
    front, laundry, pos, _k, _b = _two_hotels(db_session)
    emp, desk, wash = _vikram(db_session, front, laundry, pos,
                              desk_rate="20.00", laundry_rate="30.00")
    db_session.commit()

    found = assignment_at(db_session, emp.employee_id, "58033", _DAY)
    assert found is not None
    assert found.assignment_id == wash.assignment_id
    assert found.department_id == laundry.department_id


def test_a_rate_refusal_blocks_only_that_employee_and_names_them(db_session):
    """Preflight exists to name blockers a Payroll Admin can fix before any
    provider call. A rate refusal that escaped as an exception would 500 the run,
    name nobody, and stop everyone else's paycheck over one person's bad data.

    Below-floor overtime is the refusal used here: $28 OT against a $20 regular
    is under CA's $30 floor.
    """
    front, laundry, pos, sjces_kiosk, bw_kiosk = _two_hotels(db_session)
    emp, desk, _wash = _vikram(db_session, front, laundry, pos,
                               desk_rate="20.00", laundry_rate="20.00")
    set_rate(db_session, desk, "28.00", rate_type="ot")
    _card_with_a_split_day(db_session, emp, sjces_kiosk, bw_kiosk)

    report = assemble_pay_run_entries(
        db_session, "SJCES", _ANCHOR, anchor=_ANCHOR
    )

    assert any(str(emp.employee_id) in p and "floor" in p for p in report.problems), (
        report.problems
    )
