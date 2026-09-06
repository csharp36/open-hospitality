"""The posting engine (design D-OH27.5, .6): idempotent per
(property, date, source); corrections are reversal+repost, never edits."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from usali import gl_chart, gl_posting
from usali.models import (
    FiscalCalendar,
    GlPostingLedger,
    JournalEntry,
    JournalLine,
    UsaliFinancialFact,
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
