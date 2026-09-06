"""Trial balance from the journal (design D-OH27.9): per account per
period, exact totals, debits == credits always (the trigger guarantees it;
the report proves it end to end)."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from usali import gl_chart, gl_posting, reporting
from usali.models import UsaliFinancialFact

from tests.test_gl_posting import _first_grain, _seed_calendar


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
