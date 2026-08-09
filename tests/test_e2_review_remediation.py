"""E2 Task 7 — adversarial-review remediations, each pinned by the failure it fixes.

Every test here FAILED before its fix and the commit message records the finding
it corresponds to. Three independent reviewers (money, disclosure, migration/
tests) each in an isolated worktree produced these; the cross-report CRITICAL is
pinned in test_e2_suppression_gates.py, the set_pay_rate gaps in
test_workforce_api.py, and the rest live here.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.employees import make_employee, place, set_rate
from usali.assignments import assignment_at
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.labor import promote_timecard
from usali.models import (
    Department,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
    Timecard,
    UsaliLaborFact,
)
from usali.onboarding import terminate_employee
from usali.payroll_run import assemble_pay_run_entries
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 7, 6)
_WORKED = date(2026, 7, 7)


def _world(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HIE",
                            pms_source="OPERA", wage_jurisdiction="US-CA",
                            timezone="America/Los_Angeles"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="FD", flsa_exempt=False)
    dev = KioskDevice(property_id="HISJ", name="iPad", token_hash="a" * 64,
                      enrolled_by="x")
    db_session.add_all([pos, dev])
    db_session.flush()
    return dept, pos, dev


def _card(db_session, emp, dev, days):
    card = Timecard(employee_id=emp.employee_id, period_start=days[0],
                    period_end=days[0] + timedelta(days=13), status="approved")
    db_session.add(card)
    db_session.flush()
    for day in days:
        for ptype, hour in (("clock_in", 9), ("clock_out", 17)):
            db_session.add(Punch(
                employee_id=emp.employee_id, kiosk_device_id=dev.device_id,
                punch_type=ptype,
                punched_at=datetime(day.year, day.month, day.day, hour, tzinfo=UTC),
                business_date=day, timecard_id=card.timecard_id))
    db_session.commit()
    return card


def _costs(db_session, card):
    return {
        (f.business_date, f.department_id): Decimal(str(f.est_cost))
        for f in db_session.execute(
            select(UsaliLaborFact).where(
                UsaliLaborFact.timecard_id == card.timecard_id)
        ).scalars()
    }


# --- HIGH 1: a retroactive termination must not restate a filed period -------


def test_terminating_before_a_filed_day_is_refused(db_session):
    """MONEY reviewer HIGH 1. E2 coupled cost to the assignment predicate, so
    narrowing an assignment after promotion re-prices already-filed days to
    zero on the next re-promote (160.00 -> 0.00, dept -> Unassigned) with only a
    log warning. `terminate_employee` is the only production path that narrows
    an assignment; it must refuse to strand a day that already has a filed
    labor fact, rather than silently restating a closed Schedule 14."""
    dept, pos, dev = _world(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="HISJ",
                        full_name="Hank", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="HISJ", department_id=dept.department_id,
          position_id=pos.position_id, is_primary=True, effective_from=date(2026, 1, 5),
          pay_rate="20.00")
    card = _card(db_session, emp, dev, [_WORKED])
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    filed = _costs(db_session, card)
    assert filed[(_WORKED, dept.department_id)] == Decimal("160.0000")

    kc = InMemoryKeycloakAdmin()
    with pytest.raises(ValueError) as excinfo:
        # on_date the day BEFORE the worked+filed day -> effective_to excludes it
        terminate_employee(db_session, kc, emp.employee_id,
                           actor_subject="hr", on_date=_WORKED - timedelta(days=1))
    assert str(emp.employee_id) in str(excinfo.value)
    db_session.rollback()

    # And the filed period is untouched.
    assert _costs(db_session, card) == filed


def test_terminating_on_the_last_worked_day_still_works(db_session):
    """The complement: an ORDINARY termination on the actual last worked day
    keeps that day inside the assignment (exclusive end) and must NOT be refused.
    Freezing all terminations would be worse than the bug."""
    dept, pos, dev = _world(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="HISJ",
                        full_name="Hank", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="HISJ", department_id=dept.department_id,
          position_id=pos.position_id, is_primary=True, effective_from=date(2026, 1, 5),
          pay_rate="20.00")
    card = _card(db_session, emp, dev, [_WORKED])
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    kc = InMemoryKeycloakAdmin()
    terminate_employee(db_session, kc, emp.employee_id,
                       actor_subject="hr", on_date=_WORKED)
    db_session.commit()

    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert _costs(db_session, card)[(_WORKED, dept.department_id)] == Decimal("160.0000")


# --- HIGH 2: partial rate coverage must not pay unpriced days at a rate -------


def test_partial_rate_coverage_blocks_the_pay_run(db_session):
    """MONEY reviewer HIGH 2. `_distinct_rates` dropped `None`, so a card whose
    SOME days have no rate in force yielded one distinct rate, passed preflight,
    and paid ALL hours at it — including the days the resolver said could not be
    costed. That is the silent sample the module docstring says never happens
    ('None means cannot cost; refused rather than sampled'). Preflight must
    block, naming the employee and the uncosted day."""
    dept, pos, dev = _world(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="HISJ",
                        full_name="Hank", pay_type="hourly")
    db_session.flush()
    a = place(db_session, emp, property_id="HISJ", department_id=dept.department_id,
              position_id=pos.position_id, is_primary=True, effective_from=date(2026, 1, 5))
    # Rate in force only from the SECOND worked day.
    set_rate(db_session, a, "20.00", effective_from=date(2026, 7, 13))
    _card(db_session, emp, dev, [date(2026, 7, 6), date(2026, 7, 13)])

    report = assemble_pay_run_entries(db_session, "HISJ", date(2026, 7, 6),
                                      anchor=_ANCHOR)
    assert report.entries == [], "nothing submittable while a worked day is uncosted"
    assert any(str(emp.employee_id) in p and "2026-07-06" in p
               for p in report.problems), report.problems


# --- HIGH 3: an ambiguous placement must block the run, not 500 it ------------


def test_an_ambiguous_placement_blocks_only_that_employee(db_session):
    """MIGRATION reviewer HIGH 1. `assemble_pay_run_entries` routes through
    `assignment_at`, which raises `AmbiguousAssignmentError` — NOT a RateError —
    so the `except RateError` clause missed it and the whole run 500'd, blocking
    every other employee's paycheck and naming nobody. It must become a
    per-employee blocker like every other refusal."""
    dept, pos, dev = _world(db_session)
    laundry = Department(property_id="HISJ", name="Laundry")
    db_session.add(laundry)
    db_session.flush()
    emp = make_employee(db_session, place_primary=False, property_id="HISJ",
                        full_name="Vikram", pay_type="hourly")
    db_session.flush()
    # Two OPEN placements at one property, different starts -> overlap after the later.
    place(db_session, emp, property_id="HISJ", department_id=dept.department_id,
          position_id=pos.position_id, is_primary=True, effective_from=date(2026, 1, 5),
          pay_rate="20.00")
    place(db_session, emp, property_id="HISJ", department_id=laundry.department_id,
          is_primary=False, effective_from=date(2026, 3, 1), pay_rate="20.00")
    _card(db_session, emp, dev, [_ANCHOR])  # the grid Monday, so period_for aligns

    # Must not raise; must surface as a named blocker.
    report = assemble_pay_run_entries(db_session, "HISJ", _ANCHOR, anchor=_ANCHOR)
    assert any(str(emp.employee_id) in p for p in report.problems), report.problems


def test_assignment_at_still_refuses_two_placements(db_session):
    """The refusal itself is unchanged — `assignment_at` still raises. Only its
    HANDLING at the pay-run boundary changed. Pinned so a later 'simplification'
    that makes assignment_at pick one silently is caught here too."""
    from usali.assignments import AmbiguousAssignmentError
    dept, pos, dev = _world(db_session)
    laundry = Department(property_id="HISJ", name="Laundry")
    db_session.add(laundry)
    db_session.flush()
    emp = make_employee(db_session, place_primary=False, property_id="HISJ",
                        full_name="Vikram", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="HISJ", department_id=dept.department_id,
          is_primary=True, effective_from=date(2026, 1, 5))
    place(db_session, emp, property_id="HISJ", department_id=laundry.department_id,
          is_primary=False, effective_from=date(2026, 3, 1))
    with pytest.raises(AmbiguousAssignmentError):
        assignment_at(db_session, emp.employee_id, "HISJ", _WORKED)


# --- HIGH 4: a rate must not appear in an exception message ------------------


def test_below_floor_error_names_no_compensation_figure(db_session):
    """DISCLOSURE reviewer H1. `BelowStatutoryFloorError` interpolated the
    employee's regular rate, the stored premium, and the derived floor — and via
    the schedule projection's narrow `except` it reached an unhandled 500's
    traceback in ops logs. `_decode` in the same module deliberately omits the
    value 'because this message reaches logs'; this message must follow suit,
    naming the rate type, date, jurisdiction and citation but no dollar figure."""
    from usali.rates import BelowStatutoryFloorError, rate_on

    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="X"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="H",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    emp = make_employee(db_session, place_primary=False, property_id="HISJ",
                        full_name="Jane", pay_type="hourly")
    a = place(db_session, emp, property_id="HISJ", is_primary=True,
              effective_from=date(2026, 1, 5), pay_rate="21.00")
    set_rate(db_session, a, "20.00", rate_type="ot")  # below CA floor 31.50

    with pytest.raises(BelowStatutoryFloorError) as excinfo:
        rate_on(db_session, a.assignment_id, "ot", _WORKED)
    msg = str(excinfo.value)
    for figure in ("21.00", "20.00", "31.5", "31.50"):
        assert figure not in msg, f"{figure!r} (compensation) must not be in {msg!r}"
    assert "ot" in msg and "US-CA" in msg, "but it must stay actionable"


# --- MEDIUM: a stored holiday rate must not be silently dropped by payroll ----


def test_a_stored_holiday_rate_blocks_the_pay_run(db_session):
    """MONEY reviewer MEDIUM 3. `holiday` is a first-class RATE_TYPE, storable
    and resolvable, but consumed by nobody and refused by nobody: the estimate
    ignores it and the payroll port drops it. That is the exact silent-underpay
    the ot/dot premium block was written to stop, with the third premium type
    left out of the guard. Until a consumer models holidays, the pay run must
    refuse it like the others rather than pay around it."""
    dept, pos, dev = _world(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="HISJ",
                        full_name="Hank", pay_type="hourly")
    db_session.flush()
    a = place(db_session, emp, property_id="HISJ", department_id=dept.department_id,
              position_id=pos.position_id, is_primary=True, effective_from=date(2026, 1, 5),
              pay_rate="20.00")
    set_rate(db_session, a, "40.00", rate_type="holiday")
    _card(db_session, emp, dev, [_ANCHOR])

    report = assemble_pay_run_entries(db_session, "HISJ", _ANCHOR, anchor=_ANCHOR)
    assert report.entries == [], "a stored holiday rate the port cannot carry must block"
    assert any(str(emp.employee_id) in p and "holiday" in p for p in report.problems), (
        report.problems
    )


# --- MEDIUM: dot must be None where the jurisdiction has no double-time rule --


def test_dot_is_none_under_a_jurisdiction_with_no_double_time(db_session):
    """DISCLOSURE/MONEY MEDIUM. `HourlyRates.dot` documents itself as None where
    the jurisdiction has no double-time rule, but a STORED dot under FLSA was
    returned verbatim, contradicting the contract a future consumer would trust
    (it would then price double time at whatever was stored). Force None."""
    from usali.rates import hourly_rates_on

    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="X"))
    db_session.add(Property(property_id="TX01", org_id=1, name="TX",
                            pms_source="OPERA", wage_jurisdiction="US"))  # FLSA: no DT
    db_session.flush()
    emp = make_employee(db_session, place_primary=False, property_id="TX01",
                        full_name="Sam", pay_type="hourly")
    a = place(db_session, emp, property_id="TX01", is_primary=True,
              effective_from=date(2026, 1, 5), pay_rate="20.00")
    set_rate(db_session, a, "25.00", rate_type="dot")

    rates = hourly_rates_on(db_session, a.assignment_id, _WORKED)
    assert rates is not None
    assert rates.dot is None, "no DT rule -> no DT rate, whatever is stored"
