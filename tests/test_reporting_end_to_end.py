"""P6 end-to-end: two pipelines, one number (Task 6).

Ingests all six sample PDFs through the real `process_file` pipeline (shared
`seed_six_pdfs` fixture), then proves the payoff invariant of the phase: the
SOS's Total Operating Revenue — summed from mapped financial facts — equals the
same number derived by a completely independent pipeline:

- HISJ (Opera): the TOTAL_REVENUE DAY statistic fact promoted from the Manager
  Flash (`usali_statistic_fact`).
- SSSJ (Autoclerk): the rate-plan report's own TOTALS row, staged verbatim as
  the TOTAL/TOTAL_REVENUE segment row (`pms_daily_segment_stage`).

Each equality is asserted DB-vs-DB first (never the same query twice), and only
then pinned against the literal from the source PDFs. Consolidated test
functions keep the expensive seeding runs few.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from usali.models import PmsDailySegmentStage, UsaliStatisticFact
from usali.render import render_sos_text
from usali.reporting import coverage_report, export_rows, summary_operating_statement

_DAY = date(2026, 7, 7)


def test_cross_report_revenue_invariant(db_session, seed_six_pdfs):
    # --- HISJ (Opera): financial-fact SOS vs the statistics pipeline. The Manager
    # Flash's "Total Revenue" KPI and the Trial Balance's mapped revenue lines are
    # parsed from different PDFs by different adaptors into different fact tables —
    # they must still be the same number.
    sos_opera = summary_operating_statement(
        db_session, property_id="HISJ", business_date=_DAY
    )
    stat_value = db_session.scalar(
        select(UsaliStatisticFact.value).where(
            UsaliStatisticFact.property_id == "HISJ",
            UsaliStatisticFact.business_date == _DAY,
            UsaliStatisticFact.metric_code == "TOTAL_REVENUE",
            UsaliStatisticFact.period == "DAY",
            UsaliStatisticFact.is_prior_year.is_(False),
        )
    )
    assert stat_value is not None
    assert sos_opera.total_operating_revenue == Decimal(str(stat_value))
    assert sos_opera.total_operating_revenue == Decimal("10866.37")

    # --- SSSJ (Autoclerk): financial-fact SOS vs the rate-plan report's own TOTALS
    # row (staged verbatim as the TOTAL pseudo-segment; it is never promoted, so it
    # lives only in staging). Transaction Summary and Revenue by Rate Plan are again
    # two independent documents that must agree.
    sos_autoclerk = summary_operating_statement(
        db_session, property_id="SSSJ", business_date=_DAY
    )
    staged_total = db_session.scalar(
        select(PmsDailySegmentStage.value).where(
            PmsDailySegmentStage.pms_source == "AUTOCLERK",
            PmsDailySegmentStage.property_id == "SSSJ",
            PmsDailySegmentStage.business_date == _DAY,
            PmsDailySegmentStage.segment_code == "TOTAL",
            PmsDailySegmentStage.measure == "TOTAL_REVENUE",
            PmsDailySegmentStage.period_label == "DAY",
        )
    )
    assert staged_total is not None
    assert sos_autoclerk.total_operating_revenue == Decimal(str(staged_total))
    assert sos_autoclerk.total_operating_revenue == Decimal("6359.03")


def test_rendered_sos_text_both_properties(db_session, seed_six_pdfs):
    opera_text = render_sos_text(
        summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)
    )
    autoclerk_text = render_sos_text(
        summary_operating_statement(db_session, property_id="SSSJ", business_date=_DAY)
    )

    for text in (opera_text, autoclerk_text):
        assert "SUMMARY OPERATING STATEMENT" in text
        assert "OPERATED DEPARTMENTS" in text
        assert "TOTAL OPERATING REVENUE" in text
        assert "ROOMS SEGMENT SPLIT" in text
        assert "STATISTICS" in text

    # Opera statistics carry prior-year columns; Autoclerk's do not.
    assert "DAY PY" in opera_text
    assert "DAY PY" not in autoclerk_text


def test_coverage_and_exports(db_session, seed_six_pdfs):
    report = coverage_report(db_session)
    assert [s.pms_source for s in report.sources] == ["AUTOCLERK", "OPERA"]

    for source in report.sources:
        # Zero unmapped financial codes anywhere: every staged trx code is in the
        # dictionary and the transform banked no exceptions.
        assert source.financial.missing_codes == []
        assert source.financial.exception_count == 0
        assert source.financial.mapped_codes == source.financial.staged_codes > 0

    # The analyst worklist is nonempty: Opera's parking mapping and Autoclerk's
    # LOW-confidence brand rate plans are still awaiting review.
    opera = next(s for s in report.sources if s.pms_source == "OPERA")
    autoclerk = next(s for s in report.sources if s.pms_source == "AUTOCLERK")
    assert len(opera.financial.needs_review) > 0
    assert len(autoclerk.segments.needs_review) > 0

    # Flat exports carry every promoted fact from the six PDFs.
    assert len(export_rows(db_session, "financial", date_from=_DAY, date_to=_DAY)) == 46
    assert len(export_rows(db_session, "statistics", date_from=_DAY, date_to=_DAY)) == 103
    assert len(export_rows(db_session, "segments", date_from=_DAY, date_to=_DAY)) == 18
