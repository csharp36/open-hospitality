"""The D-OH27.9 gate, in CI from day one: the journal's revenue-side
statement must match the fact-derived SOS exactly on the seeded sample
data. The SOS is NOT re-pointed until this holds on real data too."""

from decimal import Decimal

from sqlalchemy import select

from usali import gl_chart, gl_posting, reporting
from usali.models import UsaliFinancialFact

from tests.test_gl_posting import _seed_calendar


def _post_all_grains(db_session) -> list[tuple[str, object]]:
    """Seed a fiscal calendar for every distinct property the seeded facts
    touch (`seed_six_pdfs` spans properties across the Opera/Autoclerk
    samples), then post every (property, business_date) grain found in the
    facts. Returns the sorted (property_id, business_date) pairs posted."""
    pairs = sorted(set(db_session.execute(
        select(UsaliFinancialFact.property_id, UsaliFinancialFact.business_date)
    ).all()))
    for prop in sorted({p for p, _ in pairs}):
        _seed_calendar(db_session, prop)
    for prop, day in pairs:
        gl_posting.post_and_record(
            db_session, property_id=prop, business_date=day,
            source_type="pms_daily", actor="test",
        )
    return pairs


def test_journal_and_facts_agree_on_the_seeded_samples(
    db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)

    diffs = []
    for prop in sorted({p for p, _ in pairs}):
        days = sorted(d for p, d in pairs if p == prop)
        diffs.extend(reporting.sos_journal_parity(
            db_session, property_id=prop,
            date_from=days[0], date_to=days[-1],
        ))
    assert diffs == [], f"journal disagrees with the SOS: {diffs}"


def test_parity_survives_a_reversal_chain(
    db_session, founding_org, seed_six_pdfs
):
    """Post everything, then mutate one fact and re-post its grain. The
    journal's net for that GL account must track the CURRENT fact, not the
    stale pre-reversal one — parity holds against the live facts, not
    whatever was true at first posting."""
    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)

    revenue_fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.usali_schedule_id.isnot(None),
            UsaliFinancialFact.gl_account_code.isnot(None),
        )
    ).first()
    assert revenue_fact is not None
    prop, day = revenue_fact.property_id, revenue_fact.business_date
    revenue_fact.amount = float(Decimal(str(revenue_fact.amount)) + Decimal("15.00"))
    db_session.flush()

    out = gl_posting.post_and_record(
        db_session, property_id=prop, business_date=day,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "reposted"

    diffs = []
    for p in sorted({p for p, _ in pairs}):
        days = sorted(d for pp, d in pairs if pp == p)
        diffs.extend(reporting.sos_journal_parity(
            db_session, property_id=p,
            date_from=days[0], date_to=days[-1],
        ))
    assert diffs == [], f"journal disagrees with the SOS after reversal: {diffs}"
