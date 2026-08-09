"""The B3 payoff: an approved+promoted timecard surfaces on the property's SOS as
Schedule 14 (estimated payroll expense) and Schedule 15 (hours/OT/FTE), unioned in
WITHOUT corrupting the revenue reconciliation.

`seed_six_pdfs` gives HISJ real financial facts for business date 2026-07-07 (the
sample PDFs are all dated 07.07.2026 — the same date the existing SOS tests assert
on). Labor is seeded on THAT date so the SOS has revenue to anchor on; a date the
fixture produced no revenue for would 404 with NoFactsError before labor is reached.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from tests.employees import make_employee
from usali.labor import promote_timecard
from usali.models import Department, KioskDevice, Position, Punch
from usali.reporting import summary_operating_statement
from usali.timecards import assemble_timecard

# A Monday — the workweek/period grid anchor. 2026-07-06 is the Monday of the week
# containing the fixture's 2026-07-07 business date.
_ANCHOR = date(2026, 7, 6)


def _seed_labor(db_session, business_day: date, *, pay_rate="20.00", employees=2):
    """HISJ already exists via seed_six_pdfs. Add a Housekeeping department with
    `employees` costed, approved, promoted timecards on `business_day`.

    TWO distinct employees by default so the department is NOT single-employee-
    suppressed and its est_cost actually shows on the SOS (each works 8h × $20 =
    $160, so the department totals $320). Pass `employees=1` for a solo department
    (cost suppressed) or `pay_rate=None` for the unpriced-hours case."""
    dept = Department(property_id="HISJ", name="Housekeeping",
                      usali_schedule_id=14, usali_edition=12)
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Attendant", flsa_exempt=False)
    db_session.add(pos)
    db_session.flush()
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64, enrolled_by="a")
    db_session.add(device)
    db_session.flush()
    for i in range(employees):
        emp = make_employee(db_session, property_id="HISJ", department_id=dept.department_id,
                       position_id=pos.position_id, full_name=f"Hank H{i}", pay_type="hourly",
                       pay_rate=pay_rate)
        db_session.add(emp)
        db_session.flush()
        for ptype, h in (("clock_in", 9), ("clock_out", 17)):
            db_session.add(Punch(
                employee_id=emp.employee_id, kiosk_device_id=device.device_id, punch_type=ptype,
                punched_at=datetime(business_day.year, business_day.month, business_day.day, h, tzinfo=UTC),
                business_date=business_day, photo_key=f"k/{i}/{ptype}",
            ))
        db_session.commit()
        card = assemble_timecard(db_session, emp.employee_id, business_day, anchor=_ANCHOR)
        card.status = "approved"
        card.approved_at = datetime.now(UTC)
        db_session.commit()
        promote_timecard(db_session, card, anchor=_ANCHOR)
        db_session.commit()
    return dept.department_id


def _housekeeping(sos):
    return next(line for line in sos.payroll_expense if line.department == "Housekeeping")


def test_sos_shows_labor_in_schedule_14_and_15(db_session, seed_six_pdfs):
    # A business date that exists in the seeded revenue facts (the sample PDFs).
    bdate = date(2026, 7, 7)
    _seed_labor(db_session, bdate)  # TWO employees -> cost is NOT suppressed

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=bdate)

    # Schedule 14: estimated payroll expense is the SUM of both employees
    # (2 × 8h × $20 = $320) and SHOWS (not None) — a two-employee department does
    # not leak an individual rate.
    hk = _housekeeping(sos)
    assert hk.est_cost == Decimal("320.0000")
    assert sos.payroll_expense_total == Decimal("320.0000")
    # Schedule 15: hours (2 × 8h).
    assert sos.labor_hours_total == Decimal("16.00")
    assert sos.labor_ot_hours_total == Decimal("0.00")
    # Nothing suppressed, everything priced.
    assert sos.labor_suppressed_departments == 0
    assert sos.labor_unpriced_hours == Decimal("0")
    # Labor does NOT corrupt the revenue reconciliation — that must still pass
    # (summary_operating_statement raises internally if it doesn't).


def test_single_employee_department_suppresses_cost(db_session, seed_six_pdfs):
    # A department with labor from a SINGLE distinct employee would leak that
    # employee's pay rate via est_cost / effective-hours, so the COST is hidden.
    bdate = date(2026, 7, 7)
    _seed_labor(db_session, bdate, employees=1)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=bdate)

    hk = _housekeeping(sos)
    # Cost is suppressed, but hours (operational, not the rate) still show.
    assert hk.est_cost is None
    assert hk.hours == Decimal("8.00")
    # The solo cost appears NOWHERE — not in the line, not in the total.
    assert sos.payroll_expense_total == Decimal("0")
    assert sos.labor_suppressed_departments == 1


def test_unpriced_hours_surfaced(db_session, seed_six_pdfs):
    # An hourly employee with no pay_rate is promoted with est_cost=0 but real
    # hours > 0. Those hours surface as labor_unpriced_hours (before suppression).
    bdate = date(2026, 7, 7)
    _seed_labor(db_session, bdate, employees=1, pay_rate=None)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=bdate)

    assert sos.labor_unpriced_hours == Decimal("8.00")
    # Alone in the department -> also cost-suppressed (both hold).
    hk = _housekeeping(sos)
    assert hk.est_cost is None
    assert sos.labor_suppressed_departments == 1


def test_sos_labor_sections_empty_when_no_labor(db_session, seed_six_pdfs):
    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=date(2026, 7, 7))
    assert sos.payroll_expense == []
    assert sos.payroll_expense_total == Decimal("0")
    assert sos.labor_hours_total == Decimal("0")
    assert sos.labor_suppressed_departments == 0
    assert sos.labor_unpriced_hours == Decimal("0")
