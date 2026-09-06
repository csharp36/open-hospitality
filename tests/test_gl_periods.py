"""Close/reopen semantics (design D-OH27.7): derived state, loud refusal,
reopen requires a reason, close names the gaps in both directions.

The `unposted` direction was the plan's original scope: fact dates with no
current posted `pms_daily` entry. `orphaned` is the reverse, added in
review of Task 6: dates with a current posted entry but no remaining
facts, which happens when a re-transform empties a day AFTER it was
posted — `post_and_record` returns `skipped` on a None plan, so the
ledger row still says "posted" and the gap is otherwise invisible.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.test_gl_posting import _first_grain, _seed_calendar
from usali import gl_chart, gl_posting
from usali.models import GlPeriodEvent, JournalEntry, UsaliFinancialFact


def _setup(db_session, seed_six_pdfs):
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    return prop, day


def test_close_refuses_new_postings_and_reopen_restores(
    db_session, founding_org, seed_six_pdfs
):
    prop, day = _setup(db_session, seed_six_pdfs)
    period = gl_posting.period_key_for(db_session, prop, day)
    gl_posting.close_period(db_session, property_id=prop, period_key=period,
                            actor="admin")
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "failed" and period in out.message

    gl_posting.reopen_period(db_session, property_id=prop, period_key=period,
                             actor="admin", reason="late audit pack")
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "posted"


def test_repost_into_a_closed_period_is_refused_and_the_entry_stands(
    db_session, founding_org, seed_six_pdfs
):
    prop, day = _setup(db_session, seed_six_pdfs)
    first = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    period = gl_posting.period_key_for(db_session, prop, day)
    gl_posting.close_period(db_session, property_id=prop, period_key=period,
                            actor="admin")
    fact = db_session.scalars(select(UsaliFinancialFact)).first()
    fact.amount = float(Decimal(str(fact.amount)) + Decimal("1"))
    db_session.flush()
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "failed"
    assert len(db_session.scalars(select(JournalEntry)).all()) == 1
    assert out.entry_id == first.entry_id  # the posted entry still stands


def test_reopen_requires_a_reason(db_session, founding_org, seed_six_pdfs):
    prop, day = _setup(db_session, seed_six_pdfs)
    period = gl_posting.period_key_for(db_session, prop, day)
    gl_posting.close_period(db_session, property_id=prop, period_key=period,
                            actor="admin")
    with pytest.raises(ValueError, match="reason"):
        gl_posting.reopen_period(db_session, property_id=prop,
                                 period_key=period, actor="admin", reason="")


def test_close_names_the_unposted_fact_dates(db_session, founding_org, seed_six_pdfs):
    prop, day = _setup(db_session, seed_six_pdfs)
    period = gl_posting.period_key_for(db_session, prop, day)
    gaps = gl_posting.close_period(db_session, property_id=prop,
                                   period_key=period, actor="admin")
    assert day in gaps.unposted  # nothing was posted before closing
    assert gaps.orphaned == []
    events = db_session.scalars(select(GlPeriodEvent)).all()
    assert [e.event for e in events] == ["close"]


def test_close_names_the_orphaned_posted_dates(db_session, founding_org, seed_six_pdfs):
    """A day posted, then emptied by a re-transform: the ledger still says
    posted, but no fact remains to justify it. Close must name it in the
    reverse direction, not silently accept the stale entry."""
    prop, day = _setup(db_session, seed_six_pdfs)
    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "posted"
    db_session.execute(
        UsaliFinancialFact.__table__.delete().where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
        )
    )
    db_session.flush()

    period = gl_posting.period_key_for(db_session, prop, day)
    gaps = gl_posting.close_period(db_session, property_id=prop,
                                   period_key=period, actor="admin")
    assert day in gaps.orphaned
    assert day not in gaps.unposted
