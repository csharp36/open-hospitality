"""The posting engine (design D-OH27.5, .6): idempotent per
(property, date, source); corrections are reversal+repost, never edits."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from usali import gl_chart, gl_posting
from usali.models import (
    Department,
    Employee,
    FiscalCalendar,
    GlPostingLedger,
    JournalEntry,
    JournalLine,
    Timecard,
    UsaliFinancialFact,
    UsaliLaborFact,
)


def _first_grain(session) -> tuple[str, date]:
    """The (property, business date) grain the six-PDF seed produced.

    `seed_six_pdfs` yields no handle, so the grain is read back from the
    promoted facts themselves.
    """
    prop, day = session.execute(
        select(
            UsaliFinancialFact.property_id, UsaliFinancialFact.business_date
        ).limit(1)
    ).one()
    return prop, day


def _seed_calendar(session, property_id: str) -> None:
    """The fiscal-calendar row post_and_record's period resolution requires
    (fiscal.require_config refuses without one — ADR-010)."""
    session.add(
        FiscalCalendar(
            property_id=property_id,
            calendar_type="calendar_month",
            fiscal_year_start_month=1,
            week_start_weekday=None,
        )
    )
    session.flush()


def _seed_labor(session, property_id: str, day: date, dept_costs: dict[str, str]) -> dict[str, int]:
    """Minimal labor world for the payroll_accrual source: one employee, one
    timecard, one department per name, one UsaliLaborFact per department.
    Returns {department name: department_id}. Written on the owner session,
    so the composite (org_id, ...) FKs are satisfied by the org-1 default."""
    emp = Employee(full_name="Payroll Test Worker", pay_type="hourly")
    session.add(emp)
    session.flush()
    card = Timecard(employee_id=emp.employee_id, period_start=day, period_end=day)
    session.add(card)
    session.flush()
    dept_ids: dict[str, int] = {}
    for name, cost in dept_costs.items():
        dept = Department(property_id=property_id, name=name)
        session.add(dept)
        session.flush()
        session.add(
            UsaliLaborFact(
                property_id=property_id,
                business_date=day,
                department_id=dept.department_id,
                hours=Decimal("8.00"),
                ot_hours=Decimal("0.00"),
                est_cost=Decimal(cost),
                timecard_id=card.timecard_id,
            )
        )
        dept_ids[name] = dept.department_id
    session.flush()
    return dept_ids


def _post(session, property_id, business_date):
    return gl_posting.post_and_record(
        session,
        property_id=property_id,
        business_date=business_date,
        source_type="pms_daily",
        actor="test",
    )


def test_posting_without_a_chart_is_skipped(db_session, founding_org, seed_six_pdfs):
    prop, day = _first_grain(db_session)
    out = _post(db_session, prop, day)
    assert out.status == "skipped"
    assert db_session.scalar(select(GlPostingLedger)) is None


def test_posting_without_a_fiscal_calendar_records_a_failed_row(
    db_session, founding_org, seed_six_pdfs
):
    """Chart on, calendar absent: the ADR-010 refusal must land in the
    ledger (visible), not raise out of the ingestion transaction."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    out = _post(db_session, prop, day)
    assert out.status == "failed"
    ledger = db_session.scalar(select(GlPostingLedger))
    assert ledger.status == "failed" and "fiscal calendar" in ledger.message
    assert db_session.scalar(select(JournalEntry)) is None


def test_posting_is_idempotent(db_session, founding_org, seed_six_pdfs):
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    first = _post(db_session, prop, day)
    assert first.status == "posted"
    second = _post(db_session, prop, day)
    assert second.status == "noop"
    assert second.entry_id == first.entry_id
    entries = db_session.scalars(select(JournalEntry)).all()
    assert len(entries) == 1


def test_changed_facts_reverse_and_repost(db_session, founding_org, seed_six_pdfs):
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    _post(db_session, prop, day)
    # Change the economics: bump one fact by a dollar.
    fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
        )
    ).first()
    fact.amount = float(Decimal(str(fact.amount)) + Decimal("1"))
    db_session.flush()
    out = _post(db_session, prop, day)
    assert out.status == "reposted"
    entries = db_session.scalars(
        select(JournalEntry).order_by(JournalEntry.entry_id)
    ).all()
    assert len(entries) == 3  # original, its reversal, the correction
    assert entries[1].reversal_of == entries[0].entry_id
    ledger = db_session.scalar(select(GlPostingLedger))
    assert ledger.entry_id == entries[2].entry_id and ledger.status == "posted"


def test_the_journal_nets_to_a_fresh_plan_after_any_repost(
    db_session, founding_org, seed_six_pdfs
):
    """The reversal invariant: net per account across ALL entries for the
    grain equals the current plan's lines exactly."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    _post(db_session, prop, day)
    fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
        )
    ).first()
    fact.amount = float(Decimal(str(fact.amount)) + Decimal("2.5"))
    db_session.flush()
    _post(db_session, prop, day)

    plan = gl_posting.build_pms_daily_plan(db_session, prop, day)
    want = {
        line.gl_account_code: (line.amount if line.posting == "Credit" else -line.amount)
        for line in plan.lines
    }
    got: dict[str, Decimal] = {}
    for line in db_session.scalars(select(JournalLine)).all():
        signed = line.amount if line.posting == "Credit" else -line.amount
        got[line.account_code] = got.get(line.account_code, Decimal("0")) + signed
    got = {k: v for k, v in got.items() if v != 0}
    assert got == want


def test_payroll_accrual_posts_per_department_and_is_idempotent(
    db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    _seed_labor(db_session, prop, day, {"Front Desk": "1200.50", "Housekeeping": "800.25"})

    first = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="payroll_accrual", actor="test",
    )
    assert first.status == "posted"
    lines = db_session.scalars(
        select(JournalLine).order_by(JournalLine.line_id)
    ).all()
    debits = [ln for ln in lines if ln.posting == "Debit"]
    credits = [ln for ln in lines if ln.posting == "Credit"]
    assert len(debits) == 2 and len(credits) == 1
    # Template roles: labor_wages_expense on 5000, accrued_payroll on 2200.
    assert {ln.account_code for ln in debits} == {"5000"}
    assert {ln.amount for ln in debits} == {Decimal("1200.50"), Decimal("800.25")}
    assert all("dept" in ln.memo for ln in debits)
    assert credits[0].account_code == "2200"
    assert credits[0].amount == Decimal("2000.75")

    second = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="payroll_accrual", actor="test",
    )
    assert second.status == "noop" and second.entry_id == first.entry_id
    assert len(db_session.scalars(select(JournalEntry)).all()) == 1


def test_payroll_accrual_negative_department_net_flips_to_credit(
    db_session, founding_org, seed_six_pdfs
):
    """A correction outweighing a department's day: that department's line
    moves to the credit side (positive amount), the accrued-payroll credit
    shrinks to the net total, and the entry still balances at COMMIT (the
    deferred ck_journal_entry_balanced trigger runs there)."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    _seed_labor(db_session, prop, day, {"Front Desk": "1000.00", "Spa": "-200.00"})

    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="payroll_accrual", actor="test",
    )
    assert out.status == "posted"
    db_session.commit()  # the balance trigger is deferred to commit
    lines = db_session.scalars(
        select(JournalLine).order_by(JournalLine.line_id)
    ).all()
    wage_lines = [ln for ln in lines if ln.account_code == "5000"]
    accrual_lines = [ln for ln in lines if ln.account_code == "2200"]
    assert {(ln.posting, ln.amount) for ln in wage_lines} == {
        ("Debit", Decimal("1000.00")),
        ("Credit", Decimal("200.00")),
    }
    assert [(ln.posting, ln.amount) for ln in accrual_lines] == [
        ("Credit", Decimal("800.00"))
    ]
    assert all(ln.amount > 0 for ln in lines)


def test_failed_row_recovers_to_posted_once_the_calendar_exists(
    db_session, founding_org, seed_six_pdfs
):
    """The refusal is not a dead end: fixing the named problem and re-posting
    flips the SAME ledger row to posted and clears the message."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    first = _post(db_session, prop, day)
    assert first.status == "failed"
    failed_row = db_session.scalar(select(GlPostingLedger))
    assert failed_row.status == "failed" and failed_row.message is not None

    _seed_calendar(db_session, prop)
    second = _post(db_session, prop, day)
    assert second.status == "posted"
    rows = db_session.scalars(select(GlPostingLedger)).all()
    assert len(rows) == 1
    assert rows[0].posting_ledger_id == failed_row.posting_ledger_id
    assert rows[0].status == "posted" and rows[0].message is None
    assert rows[0].entry_id == second.entry_id


def test_unmapped_gl_records_a_failed_row(db_session, founding_org, seed_six_pdfs):
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    db_session.execute(
        UsaliFinancialFact.__table__.update().values(gl_account_code=None)
    )
    db_session.flush()
    out = _post(db_session, prop, day)
    assert out.status == "failed"
    ledger = db_session.scalar(select(GlPostingLedger))
    assert ledger.status == "failed" and "no GL account code" in ledger.message
