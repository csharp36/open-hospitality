"""H5: the run stores what it paid (decision 5), and sick dollars land on
the run's own property (decision 6).

`PayRunLine.sick_hours` turns "what did this run pay as sick" from a
re-derivation over a ledger that keeps moving into a stored fact, frozen
at submit beside the combined `hours` snapshot. The apportionment then
reads THAT column: sick pay weights to the run's property (G3 refuses
any sick day attributed elsewhere, so every sick hour this run paid is
by construction this property's), while worked dollars keep splitting by
where they were clocked. Before this, a dual-property employee's sick
pay leaked onto the other hotel's Schedule 14 in proportion to worked
hours.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee, place
from tests.test_e5_provider_port import _chain_row, _payable_employee, _ssn_profile
from tests.test_g7_review_remediation import _execute
from tests.test_payroll_run import _approved_card, _seed, _shift
from tests.test_sick_pay_submission import _sick
from usali.models import (
    Department,
    KioskDevice,
    PayRunLine,
    PayRunLineProperty,
    Property,
)
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import fetch_pay_run_results

_SICK_DAY = date(2026, 7, 8)


def test_execute_stores_the_sick_share_per_line(db_session):
    """Decision 5: the line records both what it paid (`hours`, sick
    included) and how much of that was sick — a fact about THIS run,
    written at submit, never re-derived. A no-sick line stores 0 (the
    same honest value the H2 backfill gave history)."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    wanda = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, wanda, 1)
    _sick(db_session, hank, "8.00", _SICK_DAY)

    run = _execute(db_session, InMemoryPayrollProvider())
    assert run.status == "submitted"
    lines = {
        line.employee_id: line
        for line in db_session.execute(
            select(PayRunLine).where(PayRunLine.pay_run_id == run.pay_run_id)
        ).scalars()
    }
    assert Decimal(str(lines[hank].sick_hours)) == Decimal("8.00")
    assert Decimal(str(lines[hank].hours)) == Decimal("16.00")  # 8 worked + 8 sick
    assert Decimal(str(lines[wanda].sick_hours)) == Decimal("0.00")


def _dual_world(db_session):
    """One employee, two hotels: 8h clocked at HISJ, 8h clocked at SSSJ,
    8h sick attributed to the primary (HISJ — the run's property). Rate
    $20 everywhere, so the provider prices (16 + 8) x 20 = $480.00."""
    dept_id, pos_id, device_id = _seed(db_session)
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    sssj_dept = Department(property_id="SSSJ", name="Laundry")
    db_session.add(sssj_dept)
    db_session.flush()
    sssj_device = KioskDevice(property_id="SSSJ", name="iPad2",
                              token_hash="s" * 64, enrolled_by="adm")
    db_session.add(sssj_device)
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Duo D",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    place(db_session, emp, property_id="SSSJ",
          department_id=sssj_dept.department_id, is_primary=False,
          effective_from=date(2026, 1, 5), pay_rate="20.00")
    _ssn_profile(db_session, emp.employee_id)
    _chain_row(db_session, emp.employee_id, 1)
    _shift(db_session, device_id, emp.employee_id, 6, 8, 16)
    _shift(db_session, sssj_device.device_id, emp.employee_id, 7, 8, 16)
    db_session.commit()
    _approved_card(db_session, emp.employee_id)
    _sick(db_session, emp.employee_id, "8.00", _SICK_DAY)
    return emp.employee_id


def _census(db_session, run, emp_id):
    return {
        row.property_id: row
        for row in db_session.execute(
            select(PayRunLineProperty).where(
                PayRunLineProperty.pay_run_id == run.pay_run_id,
                PayRunLineProperty.employee_id == emp_id,
            )
        ).scalars()
    }


def test_a_dual_property_employees_sick_lands_on_the_paying_property(db_session):
    """Decision 6, the exact shares: worked dollars split by where they
    were clocked; sick dollars go WHOLLY to the run's property. Weights
    {HISJ: 8 worked + 8 sick, SSSJ: 8 worked} → HISJ books 16.00h /
    $320.00, SSSJ books 8.00h / $160.00. Before H5 the sick $160 split
    80/80 and half of a HISJ statutory obligation landed on SSSJ's
    Schedule 14."""
    emp_id = _dual_world(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()

    rows = _census(db_session, run, emp_id)
    assert set(rows) == {"HISJ", "SSSJ"}
    assert Decimal(str(rows["HISJ"].hours)) == Decimal("16.00")
    assert Decimal(str(rows["HISJ"].gross)) == Decimal("320.00")
    assert Decimal(str(rows["SSSJ"].hours)) == Decimal("8.00")
    assert Decimal(str(rows["SSSJ"].gross)) == Decimal("160.00")
    # Shares sum exactly to the line: nothing invented, nothing lost.
    assert (
        Decimal(str(rows["HISJ"].gross)) + Decimal(str(rows["SSSJ"].gross))
        == Decimal("480.00")
    )


def test_fetch_reads_the_stored_sick_never_the_ledger(db_session):
    """The snapshot principle, pinned in the one direction that can move
    money: the sick entry is VOIDED after submit (the paid-then-voided
    shape). The provider already priced 8 sick hours and those dollars
    are HISJ's regardless of what the ledger says now — a fetch that
    re-derived would quietly reshuffle $160 between two hotels' filed
    Schedule 14s."""
    emp_id = _dual_world(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    _sick(db_session, emp_id, "8.00", _SICK_DAY, entry_type="usage_void")

    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()

    rows = _census(db_session, run, emp_id)
    assert Decimal(str(rows["HISJ"].hours)) == Decimal("16.00")
    assert Decimal(str(rows["HISJ"].gross)) == Decimal("320.00")
    assert Decimal(str(rows["SSSJ"].gross)) == Decimal("160.00")
