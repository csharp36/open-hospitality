"""Trial balance from the journal (design D-OH27.9): per account per
period, exact totals, debits == credits always (the trigger guarantees it;
the report proves it end to end)."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from usali import gl_chart, gl_posting, reporting
from usali.models import (
    GlAccount,
    JournalEntry,
    JournalLine,
    PmsDailyFinancialStage,
    UsaliFinancialFact,
)

from tests.test_gl_posting import _first_grain, _seed_calendar, _seed_labor


def test_trial_balance_balances_and_carries_names(
    db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    prop = db_session.scalar(select(UsaliFinancialFact.property_id))
    days = sorted(set(db_session.scalars(
        select(UsaliFinancialFact.business_date).where(
            UsaliFinancialFact.property_id == prop
        )
    )))
    _seed_calendar(db_session, prop)
    for day in days:
        gl_posting.post_and_record(
            db_session, property_id=prop, business_date=day,
            source_type="pms_daily", actor="test",
        )
    period = gl_posting.period_key_for(db_session, prop, days[0])
    tb = reporting.trial_balance(db_session, property_id=prop, period_key=period)
    assert tb.total_debits == tb.total_credits > Decimal("0")
    assert all(line.name for line in tb.lines)
    assert tb.date_from <= days[0] <= tb.date_to


def test_trial_balance_survives_a_reversal_chain(
    db_session, founding_org, seed_six_pdfs
):
    """Reversal chains inflate debits AND credits equally (every reversal is
    itself a balanced entry), so the report must still balance — and the
    revenue account's balance must match what the CURRENTLY live facts say,
    not some stale sum from before the correction."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )

    # Mutate a revenue fact (schedule 1-4) so the correction lands on an
    # income account, not a tax/settlement/clearing line.
    revenue_fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
            UsaliFinancialFact.usali_schedule_id.isnot(None),
            UsaliFinancialFact.gl_account_code.isnot(None),
        )
    ).first()
    assert revenue_fact is not None
    revenue_code = revenue_fact.gl_account_code
    revenue_fact.amount = float(Decimal(str(revenue_fact.amount)) + Decimal("15.00"))
    db_session.flush()

    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "reposted"

    period = gl_posting.period_key_for(db_session, prop, day)
    tb = reporting.trial_balance(db_session, property_id=prop, period_key=period)
    assert tb.total_debits == tb.total_credits > Decimal("0")

    # The current, single source of truth for this account's revenue in the
    # period: sum the LIVE facts directly (not the journal history).
    fact_sum = sum(
        (
            Decimal(str(f.amount))
            for f in db_session.scalars(
                select(UsaliFinancialFact).where(
                    UsaliFinancialFact.property_id == prop,
                    UsaliFinancialFact.business_date >= tb.date_from,
                    UsaliFinancialFact.business_date <= tb.date_to,
                    UsaliFinancialFact.gl_account_code == revenue_code,
                )
            )
        ),
        Decimal("0"),
    )
    revenue_line = next(ln for ln in tb.lines if ln.account_code == revenue_code)
    # Revenue posts Credit (income's normal balance), so the reversal-chain
    # net (debits - credits) is the NEGATIVE of the live fact total — the
    # sign a debits-minus-credits convention gives a credit-normal account.
    assert revenue_line.debits - revenue_line.credits == -fact_sum


def test_trial_balance_raises_when_the_period_has_no_journal_lines(
    db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    period = gl_posting.period_key_for(db_session, prop, day)
    with pytest.raises(reporting.NoFactsError):
        reporting.trial_balance(db_session, property_id=prop, period_key=period)


# --- Journal-entry drill-through (OH-27 §11) ---------------------------------------


def _posted_world(db_session):
    """Chart + calendar over the six-PDF facts with the first grain posted —
    the same seeding test_trial_balance_balances_and_carries_names does,
    narrowed to one day. Returns (property, day, period)."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    return prop, day, gl_posting.period_key_for(db_session, prop, day)


def test_journal_entries_drill_returns_whole_entries(
    db_session, founding_org, seed_six_pdfs
):
    """Filter by account, return whole entries: every entry in the result
    has >=1 line on the asked account AND carries all its lines — the
    double-entry context. Amounts arrive as Decimal; ordering is
    (business_date, entry_id, line_id)."""
    prop, day, period = _posted_world(db_session)
    entry = db_session.scalars(select(JournalEntry)).one()
    all_lines = db_session.scalars(
        select(JournalLine).where(JournalLine.entry_id == entry.entry_id)
    ).all()
    assert len(all_lines) > 1  # otherwise "all its lines" proves nothing
    account = all_lines[0].account_code

    got = reporting.journal_entries(
        db_session, property_id=prop, period_key=period, account_code=account
    )
    assert [e.entry_id for e in got] == [entry.entry_id]
    (detail,) = got
    assert detail.business_date == day
    assert detail.source_type == "pms_daily"
    assert detail.posted_by == "test"
    assert detail.reversal_of is None
    assert any(ln.account_code == account for ln in detail.lines)
    # The whole entry, not just the asked account, in line_id order.
    assert [ln.line_id for ln in detail.lines] == sorted(
        ln.line_id for ln in all_lines
    )
    assert all(isinstance(ln.amount, Decimal) for ln in detail.lines)
    assert all(ln.amount > 0 for ln in detail.lines)
    assert all(ln.account_name for ln in detail.lines)
    assert all(ln.posting in ("Debit", "Credit") for ln in detail.lines)


def test_journal_entries_drill_joins_the_staged_txn(
    db_session, founding_org, seed_six_pdfs
):
    """A pms_daily line with a fact_id carries pms_trx_code / pms_trx_desc /
    source_file from its fact's stage row; a payroll_accrual line (fact_id
    None) carries None for all three and its department memo."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)

    # A line carries a fact_id only when its GL group has exactly one fact
    # (`gl_posting.JeLine` is where that decision lives), so force one:
    # move a nonzero staged fact to a fresh org-extension account nothing
    # else on the day uses.
    spare = "4900"
    db_session.add(
        GlAccount(
            org_id=1, account_code=spare, name="Marina Income",
            account_type="income", is_active=True,
        )
    )
    db_session.flush()
    fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
            UsaliFinancialFact.stage_id.is_not(None),
            UsaliFinancialFact.amount != 0,
        )
    ).first()
    assert fact is not None
    fact.gl_account_code = spare
    db_session.flush()

    gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    _seed_labor(db_session, prop, day, {"Front Desk": "1200.50"})
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="payroll_accrual", actor="test",
    )
    assert out.status == "posted"
    period = gl_posting.period_key_for(db_session, prop, day)

    stage = db_session.get(PmsDailyFinancialStage, fact.stage_id)
    got = reporting.journal_entries(
        db_session, property_id=prop, period_key=period, account_code=spare
    )
    (pms_entry,) = got
    assert pms_entry.source_type == "pms_daily"
    detail = next(ln for ln in pms_entry.lines if ln.account_code == spare)
    assert detail.fact_id == fact.fact_id
    assert (detail.pms_trx_code, detail.pms_trx_desc, detail.source_file) == (
        stage.pms_trx_code, stage.pms_trx_desc, stage.source_file
    )
    assert detail.pms_trx_code is not None and detail.source_file is not None

    # Template roles: labor_wages_expense is 5000 (the same account
    # test_payroll_accrual_posts_per_department_and_is_idempotent pins).
    payroll = reporting.journal_entries(
        db_session, property_id=prop, period_key=period, account_code="5000"
    )
    (payroll_entry,) = payroll
    assert payroll_entry.source_type == "payroll_accrual"
    wage = next(ln for ln in payroll_entry.lines if ln.account_code == "5000")
    assert wage.fact_id is None
    assert (wage.pms_trx_code, wage.pms_trx_desc, wage.source_file) == (
        None, None, None
    )
    assert "dept" in wage.memo


def test_journal_entries_drill_includes_reversals(
    db_session, founding_org, seed_six_pdfs
):
    """After a reverse-and-repost, drilling the account returns the original,
    the reversal (reversal_of set), and the correction — the audit trail the
    trial balance's nets summarize."""
    prop, day, period = _posted_world(db_session)
    revenue_fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
            UsaliFinancialFact.usali_schedule_id.isnot(None),
            UsaliFinancialFact.gl_account_code.isnot(None),
        )
    ).first()
    assert revenue_fact is not None
    revenue_code = revenue_fact.gl_account_code
    revenue_fact.amount = float(
        Decimal(str(revenue_fact.amount)) + Decimal("15.00")
    )
    db_session.flush()
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "reposted"

    got = reporting.journal_entries(
        db_session, property_id=prop, period_key=period, account_code=revenue_code
    )
    assert len(got) == 3  # original, its reversal, the correction
    original, reversal, correction = got
    assert reversal.reversal_of == original.entry_id
    assert original.reversal_of is None and correction.reversal_of is None
    assert all(
        any(ln.account_code == revenue_code for ln in e.lines) for e in got
    )
    # Same date, so entry_id is the ordering tiebreaker — asserted exactly.
    assert [e.entry_id for e in got] == sorted(e.entry_id for e in got)


def test_journal_entries_drill_of_nothing_is_nothing(
    db_session, founding_org, seed_six_pdfs
):
    """An account with no lines in the period -> [] (the line_transactions
    precedent), never an error."""
    prop, _day, period = _posted_world(db_session)
    # 2200 (accrued_payroll) is in the chart, but no labor was posted.
    assert reporting.journal_entries(
        db_session, property_id=prop, period_key=period, account_code="2200"
    ) == []
    # An account the chart has never heard of is the same nothing.
    assert reporting.journal_entries(
        db_session, property_id=prop, period_key=period, account_code="9999"
    ) == []
