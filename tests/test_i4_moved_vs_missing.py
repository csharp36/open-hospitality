"""I4 (decision 4): the sick guard learns to tell "moved" from "missing".

A retroactive primary transfer across a paid period re-routes
`sick_days_taken`'s attribution, so H6 read property A as
paid-then-voided (false) and property B as unpaid-and-blocking, with
the pay-outside instruction — the double-pay direction (the H9 deferral
row). Refinement: before branching, reconcile the employee's PERIOD-WIDE
totals — derived sick summed across ALL properties vs stored sick
summed across that period's SUBMITTED runs. Equal → the hours were
paid, only the attribution moved: a server-side log on both sides,
never a blocker. Unequal (including a PARTIAL move) → the branches
exactly as today: real unpaid hours still block by name.
"""

import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee, place
from tests.test_e5_provider_port import (
    _OPENER,
    _chain_row,
    _payable_employee,
    _ssn_profile,
)
from tests.test_payroll_run import _approved_card, _seed, _shift
from tests.test_sick_pay_submission import _sick
from usali.models import (
    Department,
    Employee,
    EmployeeAssignment,
    KioskDevice,
    PayRun,
    PayRunLine,
    PaySchedule,
    Position,
    Property,
)
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import assemble_pay_run_entries, execute_pay_run

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)        # period 2026-07-06 .. 2026-07-19
_SICK_DAY = date(2026, 7, 8)
_NEXT_PERIOD_DAY = date(2026, 7, 20)
_RECONCILED = "no additional hours are owed"


def _seed_sssj(db_session):
    """The second hotel, complete enough to run payroll: property, dept,
    position, kiosk, schedule. `_seed` built HISJ; the transfer scenario
    needs BOTH properties to have submitted runs for the same period."""
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="SSSJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Room Attendant",
                   flsa_exempt=False)
    db_session.add(pos)
    device = KioskDevice(property_id="SSSJ", name="iPad2",
                         token_hash="s" * 64, enrolled_by="adm")
    db_session.add(device)
    db_session.add(PaySchedule(
        property_id="SSSJ", frequency="biweekly", anchor=_ANCHOR,
        check_date_offset_days=5,
    ))
    db_session.flush()
    return dept.department_id, pos.position_id, device.device_id


def _payable_at_sssj(db_session, dept_id, pos_id, device_id, *,
                     name="Wanda W"):
    emp = make_employee(db_session, property_id="SSSJ",
                        department_id=dept_id, position_id=pos_id,
                        full_name=name, pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _ssn_profile(db_session, emp.employee_id)
    _chain_row(db_session, emp.employee_id, 1)
    _shift(db_session, device_id, emp.employee_id, 6, 8, 16)
    db_session.commit()
    _approved_card(db_session, emp.employee_id)
    return emp.employee_id


def _execute_at(db_session, property_id, provider, in_period=_PERIOD_DAY):
    run = execute_pay_run(
        db_session, property_id, in_period, anchor=_ANCHOR,
        provider=provider, provider_name="memory", opener=_OPENER,
        actor="test",
    )
    db_session.commit()
    return run


def _preflight(db_session, property_id, provider,
               in_period=_NEXT_PERIOD_DAY):
    return assemble_pay_run_entries(
        db_session, property_id, in_period, anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )


def _retroactive_transfer(db_session, emp_id, *, on=_SICK_DAY):
    """The audited administrative act the H9 deferral row describes: end
    the HISJ primary the day the sick was taken and open an SSSJ primary
    covering it — AFTER the period already paid. Flush between the two
    writes: inserts flush before updates, and the partial unique index
    permits only one open active primary."""
    hisj = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == emp_id,
            EmployeeAssignment.property_id == "HISJ",
            EmployeeAssignment.is_primary.is_(True),
        )
    ).scalar_one()
    hisj.effective_to = on  # exclusive end: last HISJ day is the day before
    db_session.flush()
    emp = db_session.get(Employee, emp_id)
    place(db_session, emp, property_id="SSSJ", is_primary=True,
          effective_from=on, pay_rate="20.00")
    db_session.commit()


def _two_property_paid_world(db_session):
    """Hank works and takes 8h sick at HISJ; Greta keeps HISJ populated
    after his transfer; Wanda staffs SSSJ. BOTH properties submit period-1
    runs — HISJ's pays Hank's sick — and then period-2 cards go on."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    greta = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Greta G")
    _chain_row(db_session, greta, 1)
    _sick(db_session, hank, "8.00", _SICK_DAY)
    s_dept, s_pos, s_device = _seed_sssj(db_session)
    wanda = _payable_at_sssj(db_session, s_dept, s_pos, s_device)
    provider = InMemoryPayrollProvider()
    hisj_run = _execute_at(db_session, "HISJ", provider)
    assert hisj_run.status == "submitted", hisj_run.failure_reason
    sssj_run = _execute_at(db_session, "SSSJ", provider)
    assert sssj_run.status == "submitted", sssj_run.failure_reason

    _shift(db_session, device_id, greta, 20, 8, 16)
    _shift(db_session, s_device, wanda, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, greta, period_day=_NEXT_PERIOD_DAY)
    _approved_card(db_session, wanda, period_day=_NEXT_PERIOD_DAY)
    return hank, provider


def test_a_retroactive_transfer_reconciles_and_blocks_nowhere(
    db_session, caplog
):
    """The repro itself. Hank's paid sick day moves to SSSJ retroactively.
    Period-wide, 8h taken == 8h paid: HISJ must stop logging
    paid-then-voided (false — nothing was voided) and SSSJ must not
    raise the pay-outside blocker (the double-pay instruction). Both
    sides get the reconciliation log instead."""
    hank, provider = _two_property_paid_world(db_session)
    _retroactive_transfer(db_session, hank)

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        hisj = _preflight(db_session, "HISJ", provider)
    assert not any("Hank H" in p for p in hisj.problems), hisj.problems
    assert hisj.ok, hisj.problems
    hisj_logs = [r.getMessage() for r in caplog.records]
    assert any(
        _RECONCILED in m and f"employee_id={hank}" in m for m in hisj_logs
    ), hisj_logs
    assert not any("voided after" in m for m in hisj_logs), hisj_logs
    # Server logs identify by id, never by name (I6 disclosure pin).
    assert not any("Hank H" in m for m in hisj_logs), hisj_logs

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        sssj = _preflight(db_session, "SSSJ", provider)
    assert not any("Hank" in p for p in sssj.problems), sssj.problems
    assert sssj.ok, sssj.problems
    sssj_logs = [r.getMessage() for r in caplog.records]
    assert any(
        _RECONCILED in m and f"employee_id={hank}" in m for m in sssj_logs
    ), sssj_logs
    assert not any("Hank H" in m for m in sssj_logs), sssj_logs


def test_a_genuinely_late_entry_still_blocks(db_session, caplog):
    """Reconciliation must not soften the guard: sick recorded after the
    run went out has NO paying run anywhere — period-wide 8h taken vs 0h
    paid — and the blocker stands exactly as before I4, with no
    reconciliation log muttering beside it."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    s_dept, s_pos, s_device = _seed_sssj(db_session)
    wanda = _payable_at_sssj(db_session, s_dept, s_pos, s_device)
    provider = InMemoryPayrollProvider()
    assert _execute_at(db_session, "HISJ", provider).status == "submitted"
    assert _execute_at(db_session, "SSSJ", provider).status == "submitted"
    _sick(db_session, hank, "8.00", _SICK_DAY)  # after both runs paid

    _shift(db_session, device_id, hank, 20, 8, 16)
    _shift(db_session, s_device, wanda, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)
    _approved_card(db_session, wanda, period_day=_NEXT_PERIOD_DAY)

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        report = _preflight(db_session, "HISJ", provider)
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "2026-07-08" in named[0], report.problems
    assert "was recorded after" in named[0]
    assert not report.ok
    assert not any(
        _RECONCILED in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_a_partial_move_still_blocks_with_the_residual(db_session, caplog):
    """Some hours moved, some genuinely missing: the paid 8h transfers to
    SSSJ AND a late 4h entry lands there afterwards. Period-wide 12h
    taken vs 8h paid — unequal, so NO reconciliation: SSSJ blocks naming
    its full derived figure, HISJ logs paid-then-voided exactly as
    today. Fail-loud survives the refinement."""
    hank, provider = _two_property_paid_world(db_session)
    _retroactive_transfer(db_session, hank)
    _sick(db_session, hank, "4.00", date(2026, 7, 9))  # late, lands at SSSJ

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        sssj = _preflight(db_session, "SSSJ", provider)
    named = [p for p in sssj.problems if "Hank H" in p]
    assert named and "12.00" in named[0], sssj.problems
    assert "2026-07-09" in named[0]
    assert not sssj.ok
    assert not any(
        _RECONCILED in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        hisj = _preflight(db_session, "HISJ", provider)
    assert not any("Hank H" in p for p in hisj.problems), hisj.problems
    hisj_logs = [r.getMessage() for r in caplog.records]
    assert any("voided after" in m and str(hank) in m for m in hisj_logs), (
        hisj_logs
    )
    assert not any(_RECONCILED in m for m in hisj_logs), hisj_logs


def test_an_unsubmitted_runs_lines_never_reconcile(db_session, caplog):
    """The period-wide PAID side sums SUBMITTED runs only — a draft (or
    failed) run paid nothing, whatever rows hang off it. A draft SSSJ
    run hand-carrying an 8h sick line must not launder Hank's genuinely
    unpaid hours into 'reconciled'."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()
    assert _execute_at(db_session, "HISJ", provider).status == "submitted"
    _sick(db_session, hank, "8.00", _SICK_DAY)  # after the run paid

    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    draft = PayRun(
        property_id="SSSJ", period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 19), check_date=date(2026, 7, 24),
        status="draft", provider="memory", created_by="t",
    )
    db_session.add(draft)
    db_session.flush()
    db_session.add(PayRunLine(
        pay_run_id=draft.pay_run_id, employee_id=hank,
        hours=Decimal("8.00"), sick_hours=Decimal("8.00"),
        gross="0", employee_taxes="0", employer_taxes="0", net="0",
    ))
    db_session.commit()

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        report = _preflight(db_session, "HISJ", provider)
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "2026-07-08" in named[0], report.problems
    assert not any(
        _RECONCILED in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_totals_reconcile_within_one_period_never_across(db_session, caplog):
    """The differencing oracle stays period-scoped: Hank's PRIOR period
    paid 8h of sick, and the mutant that drops the period filter would
    let that stale credit offset THIS period's genuinely late 8h. The
    period-1 blocker must stand even though his all-time paid total
    equals his all-time taken total."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()

    # Period 0 (2026-06-22 .. 07-05): worked 07-01, sick 07-02, paid.
    _shift(db_session, device_id, hank, 1, 8, 16)
    db_session.commit()
    _sick(db_session, hank, "8.00", date(2026, 7, 2))
    _approved_card(db_session, hank, period_day=date(2026, 7, 1))
    run0 = _execute_at(db_session, "HISJ", provider,
                       in_period=date(2026, 7, 1))
    assert run0.status == "submitted", run0.failure_reason

    # Period 1: the card _payable_employee approved; no sick at run time.
    run1 = _execute_at(db_session, "HISJ", provider)
    assert run1.status == "submitted", run1.failure_reason
    _sick(db_session, hank, "8.00", _SICK_DAY)  # late for period 1

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        report = _preflight(db_session, "HISJ", provider)
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "2026-07-08" in named[0], report.problems
    assert not report.ok
    assert not any(
        _RECONCILED in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]
