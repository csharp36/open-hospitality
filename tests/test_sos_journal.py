"""T4 of the SOS cutover: the statement whose numbers are the journal's
(shape C, decision note §5). Two properties are pinned here: on posted
history the journal-read statement equals the fact-read one field for
field, and after an out-of-band fact edit the journal-owned totals hold
still while the fact-derived rows visibly stop summing to them — the
failure mode the cutover exists to make detectable."""

import dataclasses
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from usali import gl_chart, reporting
from usali.models import JournalEntry, JournalLine, UsaliFinancialFact

from tests.test_gl_parity import _post_all_grains


def _property_ranges(pairs: list[tuple[str, date]]) -> list[tuple[str, date, date]]:
    return [
        (prop, min(d for p, d in pairs if p == prop), max(d for p, d in pairs if p == prop))
        for prop in sorted({p for p, _ in pairs})
    ]


def _assert_reports_equal(db_session, pairs: list[tuple[str, date]]) -> None:
    """Fact-read vs journal-read SOS, field for field, per property over its
    full posted range. Field-wise so a mismatch names the field, not just
    the dataclass."""
    for prop, first, last in _property_ranges(pairs):
        fact_rep = reporting.summary_operating_statement(
            db_session, property_id=prop, date_from=first, date_to=last
        )
        journal_rep = reporting.summary_operating_statement_from_journal(
            db_session, property_id=prop, date_from=first, date_to=last
        )
        for f in dataclasses.fields(reporting.SosReport):
            assert getattr(journal_rep, f.name) == getattr(fact_rep, f.name), (
                f"{prop}: journal-read SOS disagrees with fact-read SOS on {f.name}"
            )


def test_journal_statement_equals_fact_statement_on_the_seeded_samples(
    db_session, founding_org, seed_six_pdfs
):
    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)
    _assert_reports_equal(db_session, pairs)


def test_equality_survives_a_reversal_chain(db_session, founding_org, seed_six_pdfs):
    """Mutate one fact and RE-POST its grain (the test_gl_parity scenario):
    the journal tracks the current fact through the reversal chain, so the
    two statements must agree again."""
    from usali import gl_posting

    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)

    fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.usali_schedule_id.isnot(None),
            UsaliFinancialFact.gl_account_code.isnot(None),
        )
    ).first()
    assert fact is not None
    fact.amount = float(Decimal(str(fact.amount)) + Decimal("15.00"))
    db_session.flush()

    out = gl_posting.post_and_record(
        db_session, property_id=fact.property_id, business_date=fact.business_date,
        source_type="pms_daily", actor="test",
    )
    assert out.status == "reposted"
    _assert_reports_equal(db_session, pairs)


# One selector per journal-derived bucket, so the drift test below edits a
# fact in EACH of them: an edit confined to one bucket exercises only that
# bucket's immunity (the others agree on both derivations before the edit
# and would pass vacuously).
_BUCKET_CRITERIA = {
    "operated": (
        UsaliFinancialFact.usali_schedule_id.in_(sorted(reporting._OPERATED_SCHEDULES)),
    ),
    "misc": (UsaliFinancialFact.usali_schedule_id == reporting._MISC_INCOME_SCHEDULE,),
    "taxes": (
        UsaliFinancialFact.usali_schedule_id.is_(None),
        UsaliFinancialFact.usali_major_category == reporting.TAXES_MAJOR,
    ),
    "settlements": (
        UsaliFinancialFact.usali_schedule_id.is_(None),
        UsaliFinancialFact.usali_major_category == reporting.SETTLEMENTS_MAJOR,
    ),
    "other": (
        UsaliFinancialFact.usali_schedule_id.is_(None),
        UsaliFinancialFact.usali_major_category.not_in(
            [reporting.TAXES_MAJOR, reporting.SETTLEMENTS_MAJOR]
        ),
    ),
}


def _bucket_rows(report, bucket: str, fact) -> tuple[list, Decimal]:
    """The (fact-derived rows, journal-derived total) pair the edited fact
    lands in."""
    if bucket == "operated":
        section = next(
            s for s in report.operated_departments
            if s.sub_category == fact.usali_sub_category
        )
        return section.lines, section.total
    if bucket == "misc":
        return report.misc_income, report.misc_income_total
    if bucket == "taxes":
        return report.taxes, report.taxes_total
    if bucket == "settlements":
        return report.settlements, report.settlements_total
    return report.other, report.other_total


def test_out_of_band_fact_edit_moves_the_rows_but_not_the_totals(
    db_session, founding_org, seed_six_pdfs
):
    """The cutover's reason to exist (decision note §1): edit a fact WITHOUT
    re-posting. Every journal-derived total is unchanged, and the affected
    bucket's fact-derived rows no longer sum to its total — the drift is
    visible on the statement instead of silently absorbed. One edit PER
    bucket class, so each total's immunity assertion discriminates."""
    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)
    delta = Decimal("17.00")

    for bucket, criteria in _BUCKET_CRITERIA.items():
        fact = db_session.scalars(
            select(UsaliFinancialFact).where(
                UsaliFinancialFact.gl_account_code.isnot(None), *criteria
            )
        ).first()
        assert fact is not None, (
            f"the seeded samples carry no posted {bucket} fact; "
            f"this bucket's immunity is unpinned"
        )
        prop = fact.property_id
        first = min(d for p, d in pairs if p == prop)
        last = max(d for p, d in pairs if p == prop)

        before = reporting.summary_operating_statement_from_journal(
            db_session, property_id=prop, date_from=first, date_to=last
        )

        fact.amount = float(Decimal(str(fact.amount)) + delta)
        db_session.flush()  # no re-post: the journal still holds the old number

        after = reporting.summary_operating_statement_from_journal(
            db_session, property_id=prop, date_from=first, date_to=last
        )

        # Half one: every journal-derived total is immune to the edit.
        for name in (
            "total_operating_revenue", "misc_income_total",
            "taxes_total", "settlements_total", "other_total",
        ):
            assert getattr(after, name) == getattr(before, name), (bucket, name)
        assert [
            (s.sub_category, s.total) for s in after.operated_departments
        ] == [
            (s.sub_category, s.total) for s in before.operated_departments
        ], bucket

        # Half two: the edited bucket's rows visibly disagree with its
        # journal-owned total, by exactly the edit.
        lines, total = _bucket_rows(after, bucket, fact)
        lines_sum = sum((line.total for line in lines), Decimal("0"))
        assert lines_sum != total, bucket
        assert lines_sum - total == delta, bucket


def test_the_journal_owned_totals_set_is_closed():
    """Every revenue-side total field of SosReport must be journal-owned
    (decision note §5: "every total an operator totals against"); the labor
    and sick-pay block is the documented fact-derived complement. A future
    revenue total added to SosReport fails here until it is added to
    reporting._JOURNAL_OWNED_TOTALS — whose values the journal function
    accumulates and replaces — instead of silently staying fact-derived."""
    fact_derived_complement = {
        "payroll_expense_total",
        "labor_hours_total",
        "labor_ot_hours_total",
        "sick_pay_total",
    }
    total_fields = {
        f.name
        for f in dataclasses.fields(reporting.SosReport)
        if f.name.endswith("_total")
    } | {"total_operating_revenue"}
    assert total_fields - fact_derived_complement == set(
        reporting._JOURNAL_OWNED_TOTALS
    )


def test_an_empty_range_refuses_with_no_facts(db_session, founding_org, seed_six_pdfs):
    prop = db_session.scalars(select(UsaliFinancialFact.property_id)).first()
    assert prop is not None
    with pytest.raises(reporting.NoFactsError):
        reporting.summary_operating_statement_from_journal(
            db_session, property_id=prop, business_date=date(1999, 1, 1)
        )


def test_facts_without_posted_entries_name_gl_post(
    db_session, founding_org, seed_six_pdfs
):
    """Facts exist but nobody ran gl-post: a loud refusal naming the remedy,
    never a statement of zeros (decision note §6)."""
    fact = db_session.scalars(select(UsaliFinancialFact)).first()
    assert fact is not None
    with pytest.raises(reporting.NoPostedEntriesError, match="gl-post"):
        reporting.summary_operating_statement_from_journal(
            db_session, property_id=fact.property_id, business_date=fact.business_date
        )


def test_a_schedule_fanned_across_subs_refuses_rather_than_splits(
    db_session, founding_org, seed_six_pdfs
):
    """The chart classifies to (schedule, major) and no finer (decision note
    §4): if one schedule's facts carry two sub-categories, its journal net
    has no derivable split across the sections, and the statement must say
    so instead of guessing."""
    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)

    fact = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.usali_schedule_id.in_(
                sorted(reporting._OPERATED_SCHEDULES)
            ),
        )
    ).first()
    assert fact is not None
    siblings = db_session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == fact.property_id,
            UsaliFinancialFact.business_date == fact.business_date,
            UsaliFinancialFact.usali_schedule_id == fact.usali_schedule_id,
            UsaliFinancialFact.fact_id != fact.fact_id,
        )
    ).all()
    assert siblings, "need a second fact on the same schedule to fan it out"
    fact.usali_sub_category = "A Second Department"
    db_session.flush()

    with pytest.raises(ValueError, match="no finer"):
        reporting.summary_operating_statement_from_journal(
            db_session,
            property_id=fact.property_id,
            date_from=min(d for p, d in pairs if p == fact.property_id),
            date_to=max(d for p, d in pairs if p == fact.property_id),
        )


def test_labor_entries_in_the_range_change_nothing(
    db_session, founding_org, seed_six_pdfs
):
    """The journal-read SOS scopes to source_type='pms_daily' (decision note
    §6): a payroll_accrual entry in the same window must not move a single
    field."""
    gl_chart.seed_chart(db_session, org_id=1)
    pairs = _post_all_grains(db_session)

    ranges = _property_ranges(pairs)
    before = [
        reporting.summary_operating_statement_from_journal(
            db_session, property_id=prop, date_from=first, date_to=last
        )
        for prop, first, last in ranges
    ]

    prop, first, _ = ranges[0]
    entry = JournalEntry(
        property_id=prop, business_date=first, source_type="payroll_accrual",
        source_hash="0" * 64, posted_by="test",
    )
    db_session.add(entry)
    db_session.flush()
    db_session.add_all([
        JournalLine(entry_id=entry.entry_id, account_code="5000",
                    posting="Debit", amount=Decimal("250.00")),
        JournalLine(entry_id=entry.entry_id, account_code="2200",
                    posting="Credit", amount=Decimal("250.00")),
    ])
    db_session.flush()

    after = [
        reporting.summary_operating_statement_from_journal(
            db_session, property_id=prop, date_from=first, date_to=last
        )
        for prop, first, last in ranges
    ]
    assert after == before
