"""Unit tests for the C3 labor-variance builder (`_labor_variance`).

Seeds the fact tables DIRECTLY (unit-style — the full-pipeline payoff test is
Task 2's): a processed PayRun for 2026-07-06..2026-07-19, per-employee
PayRunLine rows (money as PLAIN decimal strings — EncryptedString encrypts on
flush), department-grain UsaliActualLaborFact rows, and B3-style UsaliLaborFact
estimate rows each backed by a Timecard (the estimate side counts distinct
employees through timecard -> employee).

World shape: Housekeeping has TWO employees (estimate 640.00, actual gross
704.00, employer burden 70.40 -> variance +64.00 = exactly 10% of the estimate,
the alert boundary); Front Desk has ONE employee (suppressed on BOTH sides,
including burden — burden is ~a fixed % of gross, so showing it would re-derive
the solo rate).
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee, place
from usali.labor import promote_timecard
from usali.models import (
    AssignmentRate,
    Department,
    Employee,
    EmployeeAssignment,
    EmployeePayrollProfile,
    KioskDevice,
    Organization,
    PayRun,
    PayRunLine,
    PayRunLineProperty,
    PaySchedule,
    Position,
    Property,
    Punch,
    Timecard,
    UsaliActualLaborFact,
    UsaliLaborFact,
)
from usali.opener import SoftwareOpener, seal_for_test
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import execute_pay_run, fetch_pay_run_results
from usali.reporting import _labor_variance, summary_operating_statement
from usali.timecards import assemble_timecard
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_PERIOD_START = date(2026, 7, 6)
_PERIOD_END = date(2026, 7, 19)
_CENTS = Decimal("0.01")


def _employee(db_session, dept_id, name):
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id, full_name=name,
                   pay_type="hourly", pay_rate="20.00")
    db_session.add(emp)
    db_session.flush()
    return emp.employee_id


def _estimate(db_session, emp_id, dept_id, *, hours, est_cost,
              business_date=date(2026, 7, 7),
              period_start=_PERIOD_START, period_end=_PERIOD_END):
    """One B3-style estimate fact backed by a Timecard for `emp_id`."""
    card = Timecard(employee_id=emp_id, period_start=period_start,
                    period_end=period_end, status="approved")
    db_session.add(card)
    db_session.flush()
    db_session.add(UsaliLaborFact(
        property_id="HISJ", business_date=business_date, department_id=dept_id,
        hours=Decimal(hours), ot_hours=Decimal("0"), est_cost=Decimal(est_cost),
        timecard_id=card.timecard_id,
    ))
    db_session.flush()


def _pay_line(db_session, run_id, emp_id, *, hours, gross, department_id,
              property_id="HISJ"):
    # Money as PLAIN decimal strings — EncryptedString encrypts on flush.
    # `department_id` is the fetch-time department SNAPSHOT, and since E1 Task 9
    # it is the ONLY department a line has: `Employee.department_id` is gone, so
    # there is nothing left to fall back to. Required rather than defaulted --
    # a None here now means "in no department", which is a different world from
    # the one these tests describe, and defaulting it would let a fixture claim
    # that world by omission.
    db_session.add(PayRunLine(
        pay_run_id=run_id, employee_id=emp_id, hours=Decimal(hours),
        gross=gross, employee_taxes="0.00", employer_taxes="0.00", net=gross,
        department_id=department_id,
    ))
    # ...and the per-property CENSUS row that `fetch_pay_run_results` writes
    # alongside it. The suppression gate counts this, not the pay line: a pay
    # line's gross is the employee's whole cross-property paycheck, so counting
    # it credited a headcount to the paying property even when every dollar
    # landed at the other one. `property_id` defaults to this file's single
    # property; the cross-property case passes it explicitly.
    db_session.add(PayRunLineProperty(
        pay_run_id=run_id, employee_id=emp_id, property_id=property_id,
        department_id=department_id, hours=Decimal(hours),
        gross=Decimal(gross), employer_burden=Decimal("0"),
    ))
    db_session.flush()


def _actual_fact(db_session, run, dept_id, *, hours, gross, burden):
    db_session.add(UsaliActualLaborFact(
        property_id="HISJ", period_start=run.period_start,
        period_end=run.period_end, department_id=dept_id,
        hours=Decimal(hours), gross=Decimal(gross),
        employer_burden=Decimal(burden), pay_run_id=run.pay_run_id,
    ))
    db_session.flush()


def _run(db_session, *, period_start=_PERIOD_START, period_end=_PERIOD_END,
         status="processed"):
    run = PayRun(property_id="HISJ", period_start=period_start,
                 period_end=period_end, check_date=period_end,
                 status=status, provider="mem", created_by="pa")
    db_session.add(run)
    db_session.flush()
    return run


def _seed_variance_world(db_session, *, hk_actual_gross="704.00"):
    """Org/property + Housekeeping (2 employees) + Front Desk (1 employee),
    one PROCESSED pay run 2026-07-06..2026-07-19 with per-employee lines,
    actual department facts, and estimate facts.

    Housekeeping: estimate 640.00 (2 x 16h x $20); actual gross
    `hk_actual_gross` split evenly across the two pay lines, employer burden =
    10% of gross. Front Desk: solo — estimate 160.00, actual gross 160.00.
    Returns (run, hk_department_id, fd_department_id).
    """
    # Idempotent org/property seeding: the portal API tests import this helper
    # on top of `seed_six_pdfs`, where HISJ already exists.
    if db_session.get(Property, "HISJ") is None:
        db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
        db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                                pms_source="OPERA", wage_jurisdiction="US-CA"))
        db_session.flush()
    hk = Department(property_id="HISJ", name="Housekeeping")
    fd = Department(property_id="HISJ", name="Front Desk")
    db_session.add_all([hk, fd])
    db_session.flush()

    run = _run(db_session)

    gross = Decimal(hk_actual_gross)
    per_line = (gross / 2).quantize(_CENTS)
    for i in range(2):
        emp_id = _employee(db_session, hk.department_id, f"Hank H{i}")
        _estimate(db_session, emp_id, hk.department_id,
                  hours="16.00", est_cost="320.0000")
        _pay_line(db_session, run.pay_run_id, emp_id,
                  hours="16.00", gross=str(per_line),
                  department_id=hk.department_id)
    _actual_fact(db_session, run, hk.department_id, hours="32.00",
                 gross=str(gross),
                 burden=str((gross * Decimal("0.10")).quantize(_CENTS)))

    solo_id = _employee(db_session, fd.department_id, "Fran F")
    _estimate(db_session, solo_id, fd.department_id,
              hours="8.00", est_cost="160.0000")
    _pay_line(db_session, run.pay_run_id, solo_id, hours="8.00", gross="160.00",
              department_id=fd.department_id)
    _actual_fact(db_session, run, fd.department_id, hours="8.00",
                 gross="160.00", burden="16.00")

    db_session.commit()
    return run, hk.department_id, fd.department_id


def test_variance_line_math_and_alert(db_session):
    # Housekeeping (2 employees): estimate 640.00, actual gross 704.00,
    # burden 70.40 -> variance +64.00 = 10% of estimate -> alert at the default 10.
    _seed_variance_world(db_session)
    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost == Decimal("640.00")
    assert line.actual_gross == Decimal("704.00")
    assert line.variance == Decimal("64.00")
    assert line.employer_burden == Decimal("70.40")
    assert line.alert is True
    assert v.periods == ["2026-07-06..2026-07-19"]


def test_under_threshold_is_not_alerted(db_session):
    # Same shape but actual 660.00 -> variance 20.00 = 3.1% -> alert False.
    _seed_variance_world(db_session, hk_actual_gross="660.00")
    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost == Decimal("640.00")
    assert line.actual_gross == Decimal("660.00")
    assert line.variance == Decimal("20.00")
    assert line.alert is False
    # Totals (Housekeeping only — Front Desk is suppressed): also under threshold.
    assert v.variance_total == Decimal("20.00")
    assert v.alert is False


def test_single_employee_department_is_suppressed_including_burden(db_session):
    # Front Desk (1 employee): est/actual/burden/variance ALL None, hours shown,
    # alert False, totals EXCLUDE it, suppressed_departments == 1.
    _seed_variance_world(db_session)
    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    solo = next(ln for ln in v.lines if ln.department == "Front Desk")
    assert solo.est_cost is None and solo.actual_gross is None
    assert solo.employer_burden is None and solo.variance is None
    assert solo.hours_actual > 0
    assert solo.alert is False
    assert v.suppressed_departments == 1
    # Complementary: totals must not let subtraction recover the solo values.
    assert v.est_total == Decimal("640.00")       # Housekeeping only
    assert v.actual_total == Decimal("704.00")
    assert v.burden_total == Decimal("70.40")
    assert v.variance_total == Decimal("64.00")


def test_no_processed_runs_means_none(db_session):
    # Even with a fully seeded world, a window touching no processed pay run
    # yields no variance block at all.
    _seed_variance_world(db_session)
    assert _labor_variance(db_session, "HISJ", date(2027, 1, 1), date(2027, 1, 31),
                           alert_pct=10) is None


def test_actual_with_no_estimate_baseline_alerts(db_session):
    # A dept with actual gross but zero estimate rows -> est 0, variance = actual,
    # alert True (no baseline is itself the alarm). Its TWO employees are counted
    # from the PayRunLine side alone, so it is NOT suppressed.
    run, _hk_id, _fd_id = _seed_variance_world(db_session)
    mt = Department(property_id="HISJ", name="Maintenance")
    db_session.add(mt)
    db_session.flush()
    for i in range(2):
        emp_id = _employee(db_session, mt.department_id, f"Mia M{i}")
        _pay_line(db_session, run.pay_run_id, emp_id, hours="5.00", gross="100.00",
                  department_id=mt.department_id)
    _actual_fact(db_session, run, mt.department_id, hours="10.00",
                 gross="200.00", burden="20.00")
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Maintenance")
    assert line.est_cost == Decimal("0.00")
    assert line.actual_gross == Decimal("200.00")
    assert line.variance == Decimal("200.00")
    assert line.alert is True
    assert v.suppressed_departments == 1  # still just Front Desk


def test_estimate_only_department_counts_timecard_employees(db_session):
    # A dept visible ONLY on the estimate side (no pay lines, no actual fact)
    # still counts its TWO timecard employees -> NOT suppressed: est shows,
    # actual 0, variance negative, and est-with-no-actual alerts (money was
    # planned, none was paid).
    _seed_variance_world(db_session)
    ld = Department(property_id="HISJ", name="Laundry")
    db_session.add(ld)
    db_session.flush()
    for i in range(2):
        emp_id = _employee(db_session, ld.department_id, f"Lou L{i}")
        _estimate(db_session, emp_id, ld.department_id,
                  hours="8.00", est_cost="160.0000")
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Laundry")
    assert line.est_cost == Decimal("320.00")
    assert line.actual_gross == Decimal("0.00")
    assert line.variance == Decimal("-320.00")
    assert line.hours_actual == Decimal("0")
    assert line.alert is True  # |-320| = 100% of the estimate
    assert v.suppressed_departments == 1  # still just Front Desk


def test_window_partially_overlapping_a_period_still_uses_the_full_period(db_session):
    # SOS window 2026-07-15..2026-07-16 (2 days inside the period) -> the block
    # still shows the FULL period's est/actual and lists the period label.
    _seed_variance_world(db_session)
    v = _labor_variance(db_session, "HISJ", date(2026, 7, 15), date(2026, 7, 16),
                        alert_pct=10)
    assert v is not None
    assert v.periods == ["2026-07-06..2026-07-19"]
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost == Decimal("640.00")
    assert line.actual_gross == Decimal("704.00")
    assert line.variance == Decimal("64.00")


def test_two_processed_runs_both_contribute_and_non_processed_do_not(db_session):
    # A SECOND processed run in the window contributes (periods are disjoint by
    # construction); draft/submitted/failed runs must contribute NOTHING even
    # when actual facts point at them.
    run1, hk_id, _fd_id = _seed_variance_world(db_session)

    run2 = _run(db_session, period_start=date(2026, 7, 20),
                period_end=date(2026, 8, 2), status="processed")
    # Department is an attribute of the ASSIGNMENT since E1 Task 9, not of the
    # person -- the same employee can be Housekeeping here and Laundry next door.
    hk_emp_ids = db_session.scalars(
        select(EmployeeAssignment.employee_id).where(
            EmployeeAssignment.department_id == hk_id
        )
    ).all()
    for emp_id in hk_emp_ids:
        _estimate(db_session, emp_id, hk_id, hours="4.00", est_cost="50.0000",
                  business_date=date(2026, 7, 21),
                  period_start=run2.period_start, period_end=run2.period_end)
        _pay_line(db_session, run2.pay_run_id, emp_id, hours="4.00", gross="50.00",
                  department_id=hk_id)
    _actual_fact(db_session, run2, hk_id, hours="8.00", gross="100.00",
                 burden="10.00")

    for status, start, end in (
        ("draft", date(2026, 8, 3), date(2026, 8, 16)),
        ("submitted", date(2026, 8, 17), date(2026, 8, 30)),
        ("failed", date(2026, 8, 31), date(2026, 9, 13)),
    ):
        ghost = _run(db_session, period_start=start, period_end=end, status=status)
        _actual_fact(db_session, ghost, hk_id, hours="99.00", gross="999.00",
                     burden="99.00")
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 9, 30),
                        alert_pct=10)
    assert v is not None
    assert v.periods == ["2026-07-06..2026-07-19", "2026-07-20..2026-08-02"]
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost == Decimal("740.00")       # 640 + 100
    assert line.actual_gross == Decimal("804.00")   # 704 + 100
    assert line.variance == Decimal("64.00")
    # The ghost runs' 999.00 facts appear NOWHERE (totals exclude solo Front Desk).
    assert v.est_total == Decimal("740.00")
    assert v.actual_total == Decimal("804.00")


def test_money_is_quantized_to_cents_and_totals_reconcile(db_session):
    # Provider money carries 4dp in the fact table; every emitted money value is
    # quantized to cents (exponent -2). variance_total reconciles to the SHOWN
    # line variances — NOT to actual_total - est_total, which no longer holds in
    # general: est/actual are suppressed per side, so a dept can contribute to
    # actual_total but not est_total (its variance is None and excluded). The
    # actual - est identity is asserted only where nothing is suppressed
    # (test_full_pipeline_variance_appears_in_the_sos).
    _seed_variance_world(db_session, hk_actual_gross="704.1290")
    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost is not None and line.actual_gross is not None
    assert line.employer_burden is not None and line.variance is not None
    assert line.actual_gross == Decimal("704.13")
    for value in (line.est_cost, line.actual_gross, line.employer_burden,
                  line.variance, v.est_total, v.actual_total, v.variance_total,
                  v.burden_total):
        assert value.as_tuple().exponent == -2
    assert v.variance_total == sum(
        (ln.variance for ln in v.lines if ln.variance is not None), Decimal("0")
    )


def test_unpriced_estimate_hours_surface(db_session):
    # An estimate fact promoted at est_cost 0 with real hours (no rate on file)
    # surfaces its hours as unpriced_hours.
    _run_id, hk_id, _fd_id = _seed_variance_world(db_session)
    emp_id = _employee(db_session, hk_id, "Nora NoRate")
    _estimate(db_session, emp_id, hk_id, hours="8.00", est_cost="0.0000")
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    assert v.unpriced_hours == Decimal("8.00")


# --- Cross-section rate-leak regressions: suppression must be PER SIDE, keyed
# to the money's own attribution. A single est∪actual union count let a dept
# with 2 union members but a 1-person estimate show that solo person's
# est_cost — and the B3 section in the SAME report (est_cost hidden, hours
# shown) let est_cost / hours recover the solo employee's encrypted pay rate.
# Symmetric for a 1-person actual (gross + burden). ------------------------------


def test_solo_estimate_multi_union_department_is_est_suppressed(db_session):
    # Spa: X has estimate facts (800.00 / 40h) AND a pay line; Y is on the pay
    # run ONLY. Actual side = 2 employees -> gross/burden SHOW. Estimate side =
    # 1 employee with est > 0 -> est_cost MUST hide (and variance with it —
    # variance + shown actual would recover it).
    run, _hk_id, _fd_id = _seed_variance_world(db_session)
    spa = Department(property_id="HISJ", name="Spa")
    db_session.add(spa)
    db_session.flush()
    x_id = _employee(db_session, spa.department_id, "Xena X")
    y_id = _employee(db_session, spa.department_id, "Yuri Y")
    _estimate(db_session, x_id, spa.department_id, hours="40.00", est_cost="800.0000")
    _pay_line(db_session, run.pay_run_id, x_id, hours="40.00", gross="880.00",
              department_id=spa.department_id)
    _pay_line(db_session, run.pay_run_id, y_id, hours="10.00", gross="200.00",
              department_id=spa.department_id)
    _actual_fact(db_session, run, spa.department_id, hours="50.00",
                 gross="1080.00", burden="108.00")
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Spa")
    assert line.est_cost is None          # the solo-estimate leak
    assert line.actual_gross == Decimal("1080.00")   # 2-person actual shows
    assert line.employer_burden == Decimal("108.00")
    assert line.variance is None          # variance + actual would recover est
    assert line.alert is False
    # Complementary per-column totals: est_total excludes Spa, actual includes it.
    assert v.est_total == Decimal("640.00")            # Housekeeping only
    assert v.actual_total == Decimal("704.00") + Decimal("1080.00")
    assert v.suppressed_departments == 2               # Front Desk + Spa


def test_solo_actual_multi_union_department_is_actual_suppressed(db_session):
    # Gym: X and Y BOTH have estimate facts (2-person estimate -> est SHOWS);
    # only X is on the pay run (1-person actual with gross > 0 -> gross AND
    # burden MUST hide; burden is ~a fixed % of gross). Variance hides too.
    run, _hk_id, _fd_id = _seed_variance_world(db_session)
    gym = Department(property_id="HISJ", name="Gym")
    db_session.add(gym)
    db_session.flush()
    x_id = _employee(db_session, gym.department_id, "Xander X")
    y_id = _employee(db_session, gym.department_id, "Yara Y")
    _estimate(db_session, x_id, gym.department_id, hours="16.00", est_cost="320.0000")
    _estimate(db_session, y_id, gym.department_id, hours="16.00", est_cost="320.0000")
    _pay_line(db_session, run.pay_run_id, x_id, hours="16.00", gross="352.00",
              department_id=gym.department_id)
    _actual_fact(db_session, run, gym.department_id, hours="16.00",
                 gross="352.00", burden="35.20")
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Gym")
    assert line.est_cost == Decimal("640.00")   # 2-person estimate shows
    assert line.actual_gross is None            # the solo-actual leak
    assert line.employer_burden is None
    assert line.variance is None
    assert line.alert is False
    # Complementary per-column totals: actual/burden exclude Gym, est includes it.
    assert v.est_total == Decimal("640.00") + Decimal("640.00")
    assert v.actual_total == Decimal("704.00")   # Housekeeping only
    assert v.burden_total == Decimal("70.40")
    assert v.suppressed_departments == 2         # Front Desk + Gym


def test_transfer_does_not_unsuppress(db_session):
    # Y is the SOLO estimate in dept Beta (est 500 -> must stay hidden). X's
    # estimate facts and pay-line snapshot both attribute to dept Alpha, but X
    # TRANSFERS to Beta before the report runs -- since E1 that is a new
    # effective-dated assignment, not a column write.
    # Keying the actual side on the CURRENT department used to add X to Beta's
    # union (2 members) and wrongly un-suppress Y's solo estimate; the
    # fetch-time snapshot keeps X counted in Alpha.
    run, _hk_id, _fd_id = _seed_variance_world(db_session)
    alpha = Department(property_id="HISJ", name="Alpha")
    beta = Department(property_id="HISJ", name="Beta")
    db_session.add_all([alpha, beta])
    db_session.flush()
    x_id = _employee(db_session, alpha.department_id, "Xavier X")
    y_id = _employee(db_session, beta.department_id, "Yolanda Y")
    _estimate(db_session, x_id, alpha.department_id, hours="20.00", est_cost="400.0000")
    _estimate(db_session, y_id, beta.department_id, hours="25.00", est_cost="500.0000")
    _pay_line(db_session, run.pay_run_id, x_id, hours="20.00", gross="440.00",
              department_id=alpha.department_id)  # fetch-time snapshot: Alpha
    _actual_fact(db_session, run, alpha.department_id, hours="20.00",
                 gross="440.00", burden="44.00")
    # The transfer happens AFTER fetch: close the Alpha assignment and open a
    # Beta one. Closing first is not incidental -- the partial unique index
    # permits only one OPEN active primary, so an overlapping pair is refused.
    transfer_on = date(2026, 7, 20)
    alpha_assignment = db_session.scalars(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == x_id,
            EmployeeAssignment.department_id == alpha.department_id,
        )
    ).one()
    alpha_assignment.effective_to = transfer_on
    db_session.flush()
    x = db_session.get(Employee, x_id)
    assert x is not None
    place(db_session, x, property_id="HISJ", department_id=beta.department_id,
          is_primary=True, effective_from=transfer_on)
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    b = next(ln for ln in v.lines if ln.department == "Beta")
    assert b.est_cost is None       # Y stays a solo estimate despite the transfer
    assert b.variance is None
    a = next(ln for ln in v.lines if ln.department == "Alpha")
    assert a.est_cost is None       # X is solo in Alpha on BOTH sides
    assert a.actual_gross is None and a.employer_burden is None
    assert a.variance is None
    # Neither solo department's money reached any total.
    assert v.est_total == Decimal("640.00")
    assert v.actual_total == Decimal("704.00")


def test_overlapping_processed_periods_count_estimate_facts_once(db_session):
    # The unique constraint pins only (property, period_start): two processed
    # runs CAN overlap. Estimate facts in the overlap must be summed once, not
    # once per run (same for unpriced hours).
    _run1, hk_id, _fd_id = _seed_variance_world(db_session)
    run2 = _run(db_session, period_start=date(2026, 7, 13),
                period_end=date(2026, 7, 26), status="processed")
    hk_emp_ids = db_session.scalars(
        select(EmployeeAssignment.employee_id).where(
            EmployeeAssignment.department_id == hk_id
        )
    ).all()
    for emp_id in hk_emp_ids:
        # 2026-07-15 is inside BOTH periods (07-06..07-19 and 07-13..07-26).
        _estimate(db_session, emp_id, hk_id, hours="5.00", est_cost="100.0000",
                  business_date=date(2026, 7, 15),
                  period_start=run2.period_start, period_end=run2.period_end)
    norate_id = _employee(db_session, hk_id, "Nate NoRate")
    _estimate(db_session, norate_id, hk_id, hours="6.00", est_cost="0.0000",
              business_date=date(2026, 7, 15),
              period_start=run2.period_start, period_end=run2.period_end)
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    assert v.periods == ["2026-07-06..2026-07-19", "2026-07-13..2026-07-26"]
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost == Decimal("840.00")   # 640 + 2 x 100, counted ONCE
    assert v.est_total == Decimal("840.00")
    assert v.unpriced_hours == Decimal("6.00")  # not 12.00


# --- Task 2: the real-pipeline payoff — the variance block reaches the SOS ----
#
# THE VARIANCE-IS-ZERO TRAP: InMemoryPayrollProvider prices gross with the SAME
# hours x rate formula as B3's estimate promote, so a naive end-to-end test
# always yields variance 0.00 and proves nothing. The payoff test manufactures
# GENUINE variance: approve + promote at $20/h (estimate 640.00), THEN raise
# both employees' rates to $22/h, THEN execute + fetch the pay run (actual
# 704.00) — exactly the real-world drift between plan-time and pay-time that
# the variance block exists to catch. Do not weaken this into a 0.00 test.

_ANCHOR = date(2026, 1, 5)  # Monday; period_for(2026-07-06) == 07-06..07-19
_OPENER = SoftwareOpener.generate(key_id="test-variance-1")


def _sealed_profile(db_session, emp_id):
    def sealed(field_name, plaintext):
        return seal_for_test(
            _OPENER.public_key(), plaintext, aad=f"{emp_id}:{field_name}".encode()
        ).to_json()

    from usali.deposit_accounts import account_slot, routing_slot
    from usali.models import DepositAccount

    db_session.add(EmployeePayrollProfile(
        employee_id=emp_id,
        ssn_sealed=sealed("ssn", b"123-45-6789"),
    ))
    db_session.add(DepositAccount(
        employee_id=emp_id, ordinal=1, allocation_type="remainder",
        allocation_value=None, account_type="checking",
        sealed_account=sealed(account_slot(1, False), b"000123456"),
        sealed_routing=sealed(routing_slot(1, False), b"021000021"),
        legacy_sealed=False,
    ))
    db_session.flush()


def _pipeline_world(db_session):
    """HISJ already exists via seed_six_pdfs. Housekeeping + position + kiosk +
    biweekly PaySchedule + TWO employees at $20/h with sealed profiles, each
    punching two 8h days (2026-07-06 and 07-07 — inside the 07-06..07-19
    period; 07-07 is the date the fixture's revenue facts anchor the SOS on).
    8h/day means no daily OT; two days means no weekly OT — the math stays
    clean. Cards approved + promoted via the direct route (the B3 path the API
    runs at approval). Returns the employee ids."""
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Room Attendant",
                   flsa_exempt=False)
    db_session.add(pos)
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="adm")
    db_session.add(device)
    db_session.add(PaySchedule(property_id="HISJ", frequency="biweekly",
                               anchor=_ANCHOR, check_date_offset_days=5))
    db_session.flush()

    ids = []
    for name in ("Hank H", "Ivy I"):
        emp = make_employee(db_session, property_id="HISJ", department_id=dept.department_id,
                       position_id=pos.position_id, full_name=name,
                       pay_type="hourly", pay_rate="20.00")
        db_session.add(emp)
        db_session.flush()
        _sealed_profile(db_session, emp.employee_id)
        for day in (6, 7):
            for ptype, h in (("clock_in", 9), ("clock_out", 17)):
                db_session.add(Punch(
                    employee_id=emp.employee_id,
                    kiosk_device_id=device.device_id, punch_type=ptype,
                    punched_at=datetime(2026, 7, day, h, tzinfo=UTC),
                    business_date=date(2026, 7, day),
                    photo_key=f"k/{emp.employee_id}-{ptype}{day}",
                ))
        db_session.commit()
        card = assemble_timecard(db_session, emp.employee_id, date(2026, 7, 6),
                                 anchor=_ANCHOR)
        card.status = "approved"
        card.approved_by = "gm"
        card.approved_at = datetime.now(UTC)
        db_session.commit()
        promote_timecard(db_session, card, anchor=_ANCHOR)
        db_session.commit()
        ids.append(emp.employee_id)
    return ids


def test_full_pipeline_variance_appears_in_the_sos(db_session, seed_six_pdfs):
    # 1. Two employees, 16h each, $20/h -> approval promotes est 640.00 (B3).
    ids = _pipeline_world(db_session)

    # 2. The payroll admin CORRECTS both rates to $22/h after approval — the
    #    drift between the promoted estimate and what payroll will price.
    #
    #    Since E2 this is a correction of the rate ROW governing those days, not
    #    a new dated rate: a raise dated later would (correctly) leave the period
    #    at $20 and produce no variance at all. Correcting a rate that was wrong
    #    when the work was done is exactly the case a variance report exists to
    #    surface -- the estimate was promoted at the old figure and payroll will
    #    submit the new one.
    for emp_id in ids:
        for rate in db_session.execute(
            select(AssignmentRate)
            .join(EmployeeAssignment,
                  EmployeeAssignment.assignment_id == AssignmentRate.assignment_id)
            .where(EmployeeAssignment.employee_id == emp_id,
                   AssignmentRate.rate_type == "regular")
        ).scalars():
            rate.amount = "22.00"
    db_session.commit()

    # 3. Real pipeline: sync + submit + fetch against the in-memory provider,
    #    which prices the SUBMITTED hours at the RAISED rate: 2 x 16h x $22.
    provider = InMemoryPayrollProvider()
    run = execute_pay_run(db_session, "HISJ", date(2026, 7, 6), anchor=_ANCHOR,
                          provider=provider, provider_name="mem",
                          opener=_OPENER, actor="pa")
    db_session.commit()
    assert run.status == "submitted"
    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    assert run.status == "processed"

    # 4. The SOS over a window covering the period carries the variance block.
    sos = summary_operating_statement(db_session, property_id="HISJ",
                                      date_from=date(2026, 7, 1),
                                      date_to=date(2026, 7, 31))
    v = sos.labor_variance
    assert v is not None
    assert v.est_total == Decimal("640.00")      # promoted at $20
    assert v.actual_total == Decimal("704.00")   # provider priced at $22
    assert v.variance_total == Decimal("64.00")  # exactly 10% of the estimate
    # Nothing is suppressed here, so the whole-block identity holds — the ONE
    # place it is still asserted (per-side suppression breaks it in general).
    assert v.variance_total == v.actual_total - v.est_total
    assert v.alert is True                       # ...the default-threshold edge
    assert v.periods == ["2026-07-06..2026-07-19"]
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost == Decimal("640.00")
    assert line.actual_gross == Decimal("704.00")
    assert line.variance == Decimal("64.00")
    assert line.alert is True


def test_sos_without_processed_runs_has_no_variance_block(db_session, seed_six_pdfs):
    # Revenue facts exist (the fixture) but no pay run touches the window:
    # the block is absent entirely, not an empty shell.
    sos = summary_operating_statement(db_session, property_id="HISJ",
                                      business_date=date(2026, 7, 7))
    assert sos.labor_variance is None


# --- G7 (money D): sick pay is on BOTH sides of the variance -----------------


def _sick_usage(db_session, emp_id, hours, on=date(2026, 7, 8)):
    from usali.models import SickLeaveLedger

    db_session.add(SickLeaveLedger(
        employee_id=emp_id, entry_type="usage",
        hours=-Decimal(hours), effective_on=on,
    ))
    db_session.commit()


def test_sick_pay_is_estimated_so_a_sick_period_shows_no_phantom_variance(
    db_session,
):
    """G3 put sick pay in the ACTUAL gross; an estimate side without it
    showed a permanent alert=True gap equal to the sick pay on every sick
    period — the 'fix both sides together or neither' shape from plan
    decision 8, fixed on one side. Both HK employees took 8h at $20: the
    estimate gains 320.00 and a correctly-paid sick period reads variance
    zero, not alarm."""
    run, hk_id, _fd_id = _seed_variance_world(
        db_session, hk_actual_gross="960.00"  # 640 worked + 320 sick
    )
    hk_emps = db_session.execute(
        select(Employee.employee_id).where(
            Employee.full_name.in_(["Hank H0", "Hank H1"]))
    ).scalars().all()
    for emp_id in hk_emps:
        _sick_usage(db_session, emp_id, "8.00")

    variance = _labor_variance(
        db_session, "HISJ", _PERIOD_START, _PERIOD_END, alert_pct=10
    )
    assert variance is not None
    hk = next(ln for ln in variance.lines if ln.department == "Housekeeping")
    assert hk.est_cost == Decimal("960.00")
    assert hk.variance == Decimal("0.00")
    assert hk.alert is False


def test_a_solo_sick_taker_hides_the_estimate_despite_two_workers(db_session):
    """The sick census is SEPARATE from the worked census (cross-report
    differencing): the SOS publishes worked and sick money under different
    per-day gates, so a combined census here would let a published SOS
    component be subtracted from this report to recover the other. One
    sick taker in a two-worker department must hide the whole estimate
    column — exactly as the SOS sick block hides that taker's cost."""
    run, hk_id, _fd_id = _seed_variance_world(db_session)
    solo_taker = db_session.execute(
        select(Employee.employee_id).where(Employee.full_name == "Hank H0")
    ).scalar_one()
    _sick_usage(db_session, solo_taker, "8.00")

    variance = _labor_variance(
        db_session, "HISJ", _PERIOD_START, _PERIOD_END, alert_pct=10
    )
    assert variance is not None
    hk = next(ln for ln in variance.lines if ln.department == "Housekeeping")
    assert hk.est_cost is None
    assert hk.variance is None
    assert hk.alert is False


def test_two_solo_sick_days_still_hide_the_estimate(db_session):
    """Per-DAY census grain, as everywhere: H0 sick Monday, H1 sick Tuesday
    is two SOLO days, not a two-person aggregate — a window split at the
    day boundary would difference each person out."""
    run, hk_id, _fd_id = _seed_variance_world(db_session)
    h0, h1 = (
        db_session.execute(
            select(Employee.employee_id).where(Employee.full_name == name)
        ).scalar_one()
        for name in ("Hank H0", "Hank H1")
    )
    _sick_usage(db_session, h0, "8.00", on=date(2026, 7, 8))
    _sick_usage(db_session, h1, "8.00", on=date(2026, 7, 9))

    variance = _labor_variance(
        db_session, "HISJ", _PERIOD_START, _PERIOD_END, alert_pct=10
    )
    assert variance is not None
    hk = next(ln for ln in variance.lines if ln.department == "Housekeeping")
    assert hk.est_cost is None
