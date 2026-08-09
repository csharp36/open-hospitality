"""G3: sick pay reaches the provider SUBMISSION — priced by the same shared
derivation the SOS reads, refused by name wherever the port cannot express
the truth.

The E4 review's F5: the Schedule 14 benefits figure was derived and
disclosed, but `assemble_pay_run_entries` submitted WORKED hours only —
used sick hours were paid to nobody through the integration. G3 closes it
under the statute check in docs/reference/ca-sick-leave.md §4a: base-hourly
IS the §246(l)(1) figure exactly because this system refuses premiums and
mixed rates, so any run that submits carries the lawful rate.
"""

import time
from datetime import date

from sqlalchemy import select

from tests.test_e5_provider_port import (
    _OPENER,
    _chain_row,
    _payable_employee,
    _ssn_profile,
)
from tests.test_payroll_run import _seed, _shift
from usali.models import SickLeaveLedger
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import assemble_pay_run_entries, execute_pay_run

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)   # period 2026-07-06 .. 2026-07-19
_SICK_DAY = date(2026, 7, 8)
_NEXT_PERIOD_DAY = date(2026, 7, 20)


def _sick(db_session, emp_id, hours, on, entry_type="usage"):
    sign = -1 if entry_type == "usage" else 1
    from decimal import Decimal

    db_session.add(SickLeaveLedger(
        employee_id=emp_id, entry_type=entry_type,
        hours=Decimal(hours) * sign, effective_on=on,
    ))
    db_session.commit()


def _assemble(db_session, provider=None):
    return assemble_pay_run_entries(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=(provider or InMemoryPayrollProvider()).capabilities(),
    )


def _entry_for(report, emp_id):
    return next(e for e in report.entries if e.employee_id == emp_id)


def test_sick_hours_ride_the_submission_at_the_run_rate(db_session):
    from decimal import Decimal

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    _sick(db_session, emp_id, "8.00", _SICK_DAY)

    report = _assemble(db_session)
    assert report.ok, report.problems
    entry = _entry_for(report, emp_id)
    assert entry.sick_hours == Decimal("8.00")
    assert entry.regular_hours == Decimal("8.00")  # the worked shift
    assert entry.hourly_rate == Decimal("20.00")


def test_sick_reaches_the_provider_and_its_gross(db_session):
    from decimal import Decimal

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    _sick(db_session, emp_id, "8.00", _SICK_DAY)
    provider = InMemoryPayrollProvider()

    run = execute_pay_run(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR, provider=provider,
        provider_name="memory", opener=_OPENER, actor="test",
    )
    db_session.commit()
    result = provider.get_pay_run(run.provider_run_id)
    # 8h worked + 8h sick at $20 = $320 — the sick half is IN the money.
    assert result.lines[0].gross == Decimal("320.00")


def test_a_sick_only_period_still_pays(db_session):
    """Out sick the whole period: no punches, no card — the sick hours are
    still owed (§246(n)) and must not vanish because the timecard loop had
    nothing to iterate. The entry carries zeros for worked hours and the
    placement's dated rate."""
    from decimal import Decimal

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    # A second employee works, so the run has a card and is otherwise alive.
    other = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, other, 1)
    # Remove Hank's punches: delete his card-producing shift by giving him
    # no worked day — recreate the world instead: Hank has profile+chain but
    # NO shift. Simplest: a third employee, sick-only.
    from tests.employees import make_employee

    solo = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Sol O",
                         pay_type="hourly", pay_rate="22.00")
    db_session.flush()
    _ssn_profile(db_session, solo.employee_id)
    _chain_row(db_session, solo.employee_id, 1)
    db_session.commit()
    _sick(db_session, solo.employee_id, "16.00", _SICK_DAY)

    report = _assemble(db_session)
    assert report.ok, report.problems
    entry = _entry_for(report, solo.employee_id)
    assert entry.sick_hours == Decimal("16.00")
    assert entry.regular_hours == Decimal("0.00")
    assert entry.ot_hours == Decimal("0.00")
    assert entry.hourly_rate == Decimal("22.00")


def test_an_unpriced_sick_day_blocks_by_name(db_session):
    """No dated regular rate in force on the day taken: paying at another
    day's rate would submit money the resolver refused to name — the
    E2 uncosted-worked-days posture, extended to sick days."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    # Usage BEFORE any rate is in force: rates start at the anchor, so pick
    # a period day only if a rate gap exists — instead, date the sick day
    # inside the period but void the rate coverage by using an employee
    # whose rate starts mid-period.
    from tests.employees import make_employee, set_rate
    from usali.models import EmployeeAssignment

    late = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Newly N",
                         pay_type="hourly")
    db_session.flush()
    assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == late.employee_id)
    ).scalar_one()
    set_rate(db_session, assignment, "21.00",
             effective_from=date(2026, 7, 10))
    _ssn_profile(db_session, late.employee_id)
    _chain_row(db_session, late.employee_id, 1)
    db_session.commit()
    _sick(db_session, late.employee_id, "8.00", _SICK_DAY)  # before 07-10

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Newly N" in p]
    assert named and "sick" in named[0] and "2026-07-08" in named[0]


def test_a_sick_day_at_a_different_rate_blocks_by_name(db_session):
    """A raise dated inside the period: worked days all fall at the new
    rate but the sick day was taken at the old one. The port carries ONE
    rate; averaging or picking would submit money nobody authorised —
    the _distinct_rates posture, extended to sick days."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    # Hank's worked shift is on 07-06 at $20. Give him a raise effective
    # 07-10 and a sick day after it: worked rate $20, sick rate $25.
    from tests.employees import set_rate
    from usali.models import AssignmentRate, EmployeeAssignment

    assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == emp_id)
    ).scalar_one()
    open_rate = db_session.execute(
        select(AssignmentRate).where(
            AssignmentRate.assignment_id == assignment.assignment_id,
            AssignmentRate.rate_type == "regular",
        )
    ).scalar_one()
    open_rate.effective_to = date(2026, 7, 10)
    set_rate(db_session, assignment, "25.00",
             effective_from=date(2026, 7, 10))
    db_session.commit()
    _sick(db_session, emp_id, "8.00", date(2026, 7, 15))  # at $25; worked at $20

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    # G7 (tests lens): pinned to the SICK-rate mismatch refusal
    # specifically — "sick or rates" was also satisfied by the generic
    # worked-days distinct-rates blocker, so this test couldn't tell
    # which refusal fired.
    assert named and "sick days taken" in named[0]
    assert "different rate" in named[0]


def test_a_voided_sick_day_is_not_paid(db_session):
    from decimal import Decimal

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    _sick(db_session, emp_id, "8.00", _SICK_DAY)
    _sick(db_session, emp_id, "8.00", _SICK_DAY, entry_type="usage_void")

    report = _assemble(db_session)
    assert report.ok, report.problems
    assert _entry_for(report, emp_id).sick_hours == Decimal("0")


def test_estimated_gross_includes_sick_for_the_carve_check(db_session):
    """Fixed deposit carve of $300: worked gross alone is $160 (8h x $20)
    and would block; worked + sick is $320 and clears. If this blocks, the
    estimate ignored sick pay — the E1 'fix both together' shape."""
    from decimal import Decimal

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1, allocation_type="amount",
               allocation_value=Decimal("300.00"))
    _chain_row(db_session, emp_id, 2)
    _sick(db_session, emp_id, "8.00", _SICK_DAY)

    report = _assemble(db_session)
    assert report.ok, report.problems


def test_late_recorded_usage_after_submission_blocks_the_next_run(db_session):
    """§246(n): sick leave is paid no later than the payday for the next
    period after it was taken. A usage entry recorded AFTER its period's
    run was submitted can never be paid by that run's window — it must
    surface as a NAMED blocker on the next preflight, not stay silently
    unpaid forever."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    provider = InMemoryPayrollProvider()
    execute_pay_run(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR, provider=provider,
        provider_name="memory", opener=_OPENER, actor="test",
    )
    db_session.commit()
    time.sleep(0.05)
    _sick(db_session, emp_id, "8.00", _SICK_DAY)  # dated in the PAID period

    # Next period: Hank works again so the next run is alive.
    _shift(db_session, device_id, emp_id, 20, 8, 16)
    db_session.commit()
    from tests.test_payroll_run import _approved_card

    _approved_card(db_session, emp_id, period_day=_NEXT_PERIOD_DAY)

    report = assemble_pay_run_entries(
        db_session, "HISJ", _NEXT_PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "sick" in named[0] and "2026-07-08" in named[0]
    # G7 (tests lens): pinned to the §246(n) late-recording refusal
    # specifically, not any message containing "after".
    assert "was recorded after" in named[0] and "already submitted" in named[0]


def test_pre_run_history_does_not_block(db_session):
    """Usage dated in a period that NEVER had a run (the operator-era
    posture: sick paid by hand through the provider UI) must not become a
    permanent blocker — G3 changes payment going FORWARD."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    _sick(db_session, emp_id, "8.00", date(2026, 6, 10))  # old, never-run period

    report = _assemble(db_session)
    assert report.ok, report.problems
