"""E2 Task 6: re-verify every suppression gate against a RATE-DERIVED population.

Four gates decide whether a department's money is disclosed, and all four count
the same thing: how many DISTINCT PRICED employees the number came from. Fewer
than two and the figure IS one person's pay, recoverable as cost / hours.

  1. `_labor_sections`      -- Schedule 14/15 on the SOS
  2. `_labor_variance` est  -- the estimate column
  3. `_labor_variance` act  -- the actual column (separate gate, separate keying)
  4. `schedule_api`         -- the projection panel

E2 changed what "priced" MEANS. Before it, unpriced had exactly one cause:
`Employee.pay_rate IS NULL`. Now it has three, because a rate belongs to a date
range -- no rate row at all, a rate that starts AFTER the day worked, and a rate
that ENDED before it. The last two could not exist before this phase, and a gate
that happens to handle the first does not thereby handle them.

The plan is explicit that a fix applied to one gate has repeatedly not reached
the others: that mistake produced six Criticals across C3, D1 and D3. So each
gate is exercised on its own here rather than assumed to inherit.

## A note on mutation-checking these gates

A mutation only proves something if it pushes toward DISCLOSURE. During Task 4 I
mutated the actual-side gate by removing `gross > 0` from `actual_hidden` -- and
nothing failed, because that suppresses MORE, not less. A test asserting
`is None` cannot notice. Every mutation named below un-hides.
"""

from datetime import UTC, date, datetime
from decimal import Decimal


from tests.employees import make_employee, set_rate
from usali.labor import promote_timecard
from usali.models import (
    Department,
    EmployeeAssignment,
    KioskDevice,
    PayRun,
    PayRunLineProperty,
    Position,
    Punch,
    UsaliActualLaborFact,
)
from usali.reporting import _labor_variance, summary_operating_statement
from usali.timecards import assemble_timecard
from sqlalchemy import select

# The Monday anchoring the workweek grid for the fixture's 2026-07-07 facts.
_ANCHOR = date(2026, 7, 6)
_WORKED = date(2026, 7, 7)
_LATER = date(2026, 7, 8)


def _housekeeping(db_session):
    dept = Department(property_id="HISJ", name="Housekeeping",
                      usali_schedule_id=14, usali_edition=12)
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Attendant",
                   flsa_exempt=False)
    db_session.add(pos)
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="a")
    db_session.add(device)
    db_session.flush()
    return dept, pos, device


def _worker(db_session, dept, pos, device, name, *, rate, rate_from=None,
            days=(_WORKED,), pay_type="hourly"):
    """One employee with an 8h day per entry in `days`, promoted.

    `rate_from` defaults to the placement's own start, i.e. in effect for every
    day worked. Passing a LATER date is the E2 case: a rate row exists but
    governs none of the days actually worked, so the hours promote at
    `est_cost = 0` and this person is not part of the priced population.
    """
    emp = make_employee(db_session, property_id="HISJ",
                        department_id=dept.department_id,
                        position_id=pos.position_id, full_name=name,
                        pay_type=pay_type)
    db_session.flush()
    if rate is not None:
        assignment = db_session.execute(
            select(EmployeeAssignment).where(
                EmployeeAssignment.employee_id == emp.employee_id
            )
        ).scalar_one()
        set_rate(db_session, assignment, rate, effective_from=rate_from)
    for day in days:
        for ptype, h in (("clock_in", 9), ("clock_out", 17)):
            db_session.add(Punch(
                employee_id=emp.employee_id, kiosk_device_id=device.device_id,
                punch_type=ptype,
                punched_at=datetime(day.year, day.month, day.day, h, tzinfo=UTC),
                business_date=day, photo_key=f"k/{name}/{day}/{ptype}",
            ))
    db_session.commit()
    card = assemble_timecard(db_session, emp.employee_id, days[0], anchor=_ANCHOR)
    card.status = "approved"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    return emp


def _line(sos):
    return next(ln for ln in sos.payroll_expense if ln.department == "Housekeeping")


# --- GATE 1: _labor_sections (Schedule 14/15) --------------------------------


def test_sections_gate_a_rate_not_yet_in_effect_is_not_a_priced_employee(
    db_session, seed_six_pdfs
):
    """Two employees in the department, but only ONE is priced: the other's rate
    starts the day AFTER the day worked, so their hours promoted at zero.

    A headcount of two would disclose a figure that IS the first employee's pay,
    with their hours on the very next line -- 160.00 / 8.00 = $20/h.

    Mutation: drop `and Decimal(str(f.est_cost)) > 0` from the gate -> the
    unpriced employee counts, the department reads two, and est_cost discloses.
    """
    dept, pos, device = _housekeeping(db_session)
    _worker(db_session, dept, pos, device, "Priced P", rate="20.00")
    _worker(db_session, dept, pos, device, "Later L", rate="31.00",
            rate_from=_LATER)

    sos = summary_operating_statement(db_session, property_id="HISJ",
                                      business_date=_WORKED)
    line = _line(sos)

    assert line.est_cost is None, (
        "one priced employee: the cost IS their pay, recoverable from the hours"
    )
    assert Decimal(str(line.hours)) > 0, "hours still show — Schedule 15 stays complete"


def test_sections_gate_the_suppressed_cost_is_in_no_total(db_session, seed_six_pdfs):
    """Complementary totals. If the department is hidden but its cost still
    reached `payroll_expense_total`, then total minus the shown lines re-derives
    exactly the hidden figure and the suppression is decorative."""
    dept, pos, device = _housekeeping(db_session)
    _worker(db_session, dept, pos, device, "Priced P", rate="20.00")
    _worker(db_session, dept, pos, device, "Later L", rate="31.00",
            rate_from=_LATER)

    sos = summary_operating_statement(db_session, property_id="HISJ",
                                      business_date=_WORKED)
    shown = sum(
        (Decimal(str(ln.est_cost)) for ln in sos.payroll_expense
         if ln.est_cost is not None),
        Decimal("0"),
    )
    assert Decimal(str(sos.payroll_expense_total)) == shown, (
        "the hidden department's cost must not be recoverable as total - shown"
    )


def test_sos_windows_cannot_be_differenced_to_isolate_an_employee(
    db_session, seed_six_pdfs
):
    """THE FOURTH INSTANCE of the differencing class -- after C3, D1 and D3.

    D3's recorded lesson is general: *any aggregate that varies with a
    caller-controlled parameter is a differencing oracle candidate; suppression
    must consider the MARGINAL population between adjacent parameter values, or
    the aggregate must be made parameter-invariant.* That lesson was applied to
    the schedule projection's `as_of` and never to the SOS's date window --
    which `/api/sos` exposes directly as `from` / `to`.

    Alice works the 7th and the 8th; Bob works only the 7th. BOTH windows below
    contain two priced employees, so the static gate discloses both, and the
    difference is Alice's 8th alone:

        (522.72 - 337.36) / (24.00 - 16.00) = 23.17 = Alice's exact rate

    Reproduced live before this test was written, so it is a finding rather than
    a theory. Revenue exists only on the 7th and the SOS 404s on a revenue-less
    window, so both windows must contain it -- which is why the attack is framed
    as widening rather than narrowing.

    FIXED in E2 Task 6 by `_discloses`: >= 2 distinct priced employees on EVERY
    business date carrying cost, not merely across the window. Days are the
    finest granularity a caller can select, so if every day is a >= 2-person
    aggregate then every difference between windows is a sum of >= 2-person
    aggregates.
    """
    dept, pos, device = _housekeeping(db_session)
    _worker(db_session, dept, pos, device, "Alice A", rate="23.17",
            days=(_WORKED, _LATER))
    _worker(db_session, dept, pos, device, "Bob B", rate="19.00",
            days=(_WORKED,))

    narrow = _line(summary_operating_statement(
        db_session, property_id="HISJ", business_date=_WORKED))
    wide = _line(summary_operating_statement(
        db_session, property_id="HISJ", date_from=_WORKED, date_to=_LATER))

    if narrow.est_cost is None or wide.est_cost is None:
        return  # a suppressed window offers nothing to difference

    delta_cost = Decimal(str(wide.est_cost)) - Decimal(str(narrow.est_cost))
    delta_hours = Decimal(str(wide.hours)) - Decimal(str(narrow.hours))
    assert delta_hours <= 0 or delta_cost / delta_hours != Decimal("23.17"), (
        "differencing two disclosed windows recovered Alice's exact hourly rate"
    )


# --- GATE 2: _labor_variance, estimate side ----------------------------------


def test_variance_estimate_gate_counts_only_priced_employees(
    db_session, seed_six_pdfs
):
    """The same population question, asked by a different function over a
    different table -- and historically answered correctly in one and not the
    other. `_labor_sections` was fixed first and this gate was not, which is how
    four of the six instances survived.

    Mutation: drop `and Decimal(str(lf.est_cost)) > 0` here -> est_cost discloses.
    """
    dept, pos, device = _housekeeping(db_session)
    _worker(db_session, dept, pos, device, "Priced P", rate="20.00")
    _worker(db_session, dept, pos, device, "Later L", rate="31.00",
            rate_from=_LATER)
    # The variance section only exists where a PROCESSED pay run intersects the
    # window -- it is estimate-vs-actual, and with no run there is no actual to
    # compare against. The run carries no lines, so the actual side is zero and
    # only the ESTIMATE gate is under test here.
    db_session.add(PayRun(
        property_id="HISJ", period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 19), check_date=date(2026, 7, 24),
        status="processed", provider="mem", created_by="pa",
    ))
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.est_cost is None, "one priced employee behind the estimate"


# --- GATE 3: _labor_variance, actual side ------------------------------------


def test_variance_actual_gate_counts_only_employees_with_money_here(
    db_session, seed_six_pdfs
):
    """The actual side is keyed to the pay-run census, NOT to rates, so E2 does
    not change how it decides. Re-verified anyway because the plan's whole point
    is that inheritance between gates is what keeps failing.

    A census row with zero gross is a headcount without a dollar: that person's
    money landed at the other property, so counting them here lets ONE person's
    gross be published.

    Mutation: `if Decimal(str(census.gross or 0)) > 0:` -> `if True:` ->
    actual_gross discloses.
    """
    dept, _pos, _device = _housekeeping(db_session)
    paid = make_employee(db_session, property_id="HISJ",
                         department_id=dept.department_id,
                         full_name="Paid P", pay_type="hourly")
    zero = make_employee(db_session, property_id="HISJ",
                         department_id=dept.department_id,
                         full_name="Zero Z", pay_type="hourly")
    db_session.flush()
    run = PayRun(property_id="HISJ", period_start=date(2026, 7, 6),
                 period_end=date(2026, 7, 19), check_date=date(2026, 7, 24),
                 status="processed", provider="mem", created_by="pa")
    db_session.add(run)
    db_session.flush()
    for emp, gross in ((paid, "160.00"), (zero, "0.00")):
        db_session.add(PayRunLineProperty(
            pay_run_id=run.pay_run_id, employee_id=emp.employee_id,
            property_id="HISJ", department_id=dept.department_id,
            hours=Decimal("8.00"), gross=Decimal(gross),
            employer_burden=Decimal("0"),
        ))
    db_session.add(UsaliActualLaborFact(
        property_id="HISJ", period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 19), department_id=dept.department_id,
        hours=Decimal("8.00"), gross=Decimal("160.00"),
        employer_burden=Decimal("16.00"), pay_run_id=run.pay_run_id,
    ))
    db_session.commit()

    v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Housekeeping")
    assert line.actual_gross is None, "a zero-gross census row is not a second person"
    assert line.employer_burden is None, (
        "burden is ~a fixed pct of gross, so publishing it re-derives the gross"
    )


def _processed_run(db_session, dept, start, end, lines):
    """A processed pay run with a per-property census and a department fact.

    `lines` is (employee, gross) pairs — the census the actual-side gate counts.
    """
    run = PayRun(property_id="HISJ", period_start=start, period_end=end,
                 check_date=end, status="processed", provider="mem",
                 created_by="pa")
    db_session.add(run)
    db_session.flush()
    total = Decimal("0")
    for emp, gross in lines:
        db_session.add(PayRunLineProperty(
            pay_run_id=run.pay_run_id, employee_id=emp.employee_id,
            property_id="HISJ", department_id=dept.department_id,
            hours=Decimal("8.00"), gross=Decimal(gross),
            employer_burden=Decimal("0"),
        ))
        total += Decimal(gross)
    db_session.add(UsaliActualLaborFact(
        property_id="HISJ", period_start=start, period_end=end,
        department_id=dept.department_id, hours=Decimal("8.00"),
        gross=total, employer_burden=(total / 10).quantize(Decimal("0.01")),
        pay_run_id=run.pay_run_id,
    ))
    db_session.flush()
    return run


def test_variance_windows_cannot_be_differenced_to_isolate_an_employee(
    db_session, seed_six_pdfs
):
    """The SAME differencing class as the SOS window, one unit coarser.

    `_labor_variance` selects the PROCESSED pay runs intersecting the window and
    counts the priced population over their UNION, so the marginal unit is a
    whole pay run rather than a day. Run A has two people in this department;
    run B has one. Both windows clear a union-based gate — A alone counts two,
    A+B counts three — and the difference is run B's single employee.

    Found by asking whether the SOS leak's shape recurred here rather than
    assuming the fix would carry: not carrying it is exactly how six Criticals
    survived across C3, D1 and D3.
    """
    dept, _pos, _device = _housekeeping(db_session)
    a1 = make_employee(db_session, property_id="HISJ",
                       department_id=dept.department_id, full_name="A One",
                       pay_type="hourly")
    a2 = make_employee(db_session, property_id="HISJ",
                       department_id=dept.department_id, full_name="A Two",
                       pay_type="hourly")
    solo = make_employee(db_session, property_id="HISJ",
                         department_id=dept.department_id, full_name="Solo S",
                         pay_type="hourly")
    db_session.flush()
    _processed_run(db_session, dept, date(2026, 7, 6), date(2026, 7, 19),
                   [(a1, "100.00"), (a2, "100.00")])
    _processed_run(db_session, dept, date(2026, 7, 20), date(2026, 8, 2),
                   [(solo, "777.00")])
    db_session.commit()

    def gross(to_date):
        v = _labor_variance(db_session, "HISJ", date(2026, 7, 1), to_date,
                            alert_pct=10)
        assert v is not None
        ln = next(x for x in v.lines if x.department == "Housekeeping")
        return ln.actual_gross

    first_run_only = gross(date(2026, 7, 19))
    both_runs = gross(date(2026, 8, 2))

    if first_run_only is None or both_runs is None:
        return  # a suppressed window offers nothing to difference
    assert both_runs - first_run_only != Decimal("777.00"), (
        "differencing two disclosed windows recovered the solo employee's gross"
    )


def test_the_two_reports_over_one_fact_set_suppress_in_lockstep(
    db_session, seed_six_pdfs
):
    """THE SEVENTH INSTANCE, and the one that got past the Task 6 fix.

    `_labor_sections` and `_labor_variance`'s estimate side publish the SAME
    `UsaliLaborFact` rows. Task 6 gave each report the finest unit IT selects —
    a day for the SOS, a pay run for the variance — reasoning about them one at
    a time. But a run whose fortnight holds two priced people passes a run-wide
    gate even when one DAY inside it was solitary, so the variance column
    republished precisely what the SOS section had just withheld:

        variance est (7/7..7/12, per run)  907.28
        SOS section  (7/7..7/8,  per day)  656.00   hours 32.00
        SOS section  (7/7..7/12, per day)  None     hours 40.00
        (907.28 - 656.00) / (40.00 - 32.00) = 31.41 = Solo's exact rate

    Both figures ship in ONE `SosReport` from `GET /api/sos`, behind ordinary
    property access rather than payroll-admin.

    THE GENERALISED RULE: the suppression unit must be the finest grain ANY
    report exposes over a fact set, never the finest grain THIS report happens
    to select. Two reports over one fact set are one disclosure surface.

    Asserted as an INVARIANT rather than a scenario: whenever the SOS section
    suppresses a department, the variance estimate for a window covering the
    same facts must suppress too. A scenario test pins one arithmetic accident;
    the invariant pins the relationship, and it is the relationship that six
    previous instances kept breaking.
    """
    dept, pos, device = _housekeeping(db_session)
    solo_day = date(2026, 7, 12)
    _worker(db_session, dept, pos, device, "Alice A", rate="20.00",
            days=(_WORKED, _LATER))
    _worker(db_session, dept, pos, device, "Bob B", rate="21.00",
            days=(_WORKED, _LATER))
    _worker(db_session, dept, pos, device, "Solo S", rate="31.41",
            days=(solo_day,))
    db_session.add(PayRun(
        property_id="HISJ", period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 19), check_date=date(2026, 7, 24),
        status="processed", provider="mem", created_by="pa",
    ))
    db_session.commit()

    section = _line(summary_operating_statement(
        db_session, property_id="HISJ", date_from=_WORKED, date_to=solo_day))
    v = _labor_variance(db_session, "HISJ", _WORKED, solo_day, alert_pct=10)
    assert v is not None
    variance = next(x for x in v.lines if x.department == "Housekeeping")

    assert section.est_cost is None, "the solo day must suppress the SOS section"
    assert variance.est_cost is None, (
        "the variance estimate reads the SAME facts; publishing them re-opens "
        "what the section closed, and the difference is one person's day"
    )
