"""HotelKey through the real pipeline: four synthetic exports, one property.

Format fidelity is the ceiling (design §5): the figures are invented, so
nothing here claims a HotelKey number is classified correctly. What IS pinned:
every export lands, statistics and AR balances promote, financial rows stage
and never post, and the whole thing is one batch per file.
"""
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from usali import gl_chart
from usali.db import make_session_factory
from usali.ingestion import ProcessingError, process_file
from usali.mapping.property_registry import create_first_property, seed_properties
from usali.tenancy import bind_org_context
from usali.transform import transform
from usali.mapping.loader import load_mappings
from usali.mapping.schedules import seed_schedules
from usali.models import (
    GlPostingLedger,
    IngestBatch,
    IngestionCoverage,
    JournalEntry,
    MappingException,
    PmsDailyFinancialStage,
    PmsDailyStatisticStage,
    PmsLedgerBalanceStage,
    UsaliFinancialFact,
    UsaliLedgerBalanceFact,
    UsaliStatisticFact,
)

from tests.test_gl_posting import _seed_calendar

FIX = Path("tests/fixtures/hotelkey")
PDF = Path("docs/reference/samples/HotelKey - Hotel Statistics (mock).pdf")
BD = date(2026, 8, 13)


@pytest.fixture
def seeded(db_session, founding_org):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/hotelkey.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()
    return db_session


def _ingest(session, tmp_path, src: Path):
    drop = tmp_path / src.name
    shutil.copy(src, drop)
    return process_file(
        session, drop, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail"
    )


def test_all_four_exports_land_for_the_seeded_property(seeded, tmp_path):
    results = [_ingest(seeded, tmp_path, p) for p in (
        PDF,
        FIX / "Settlement By Payment Type.xlsx",
        FIX / "All Payments.xlsx",
        FIX / "AR Invoice Aging.xlsx",
    )]
    assert [(r.pms_source, r.report_type, r.property_id) for r in results] == [
        ("HOTELKEY", "hotel_statistics", "HKDEMO"),
        ("HOTELKEY", "settlement", "HKDEMO"),
        ("HOTELKEY", "all_payments", "HKDEMO"),
        ("HOTELKEY", "ar_aging", "HKDEMO"),
    ]
    assert all(r.business_date == BD for r in results)
    assert all(r.destination.parent == tmp_path / "done" for r in results)
    landed = set(seeded.scalars(select(IngestionCoverage.report_type).where(
        IngestionCoverage.property_id == "HKDEMO", IngestionCoverage.business_date == BD)))
    assert landed == {"hotel_statistics", "settlement", "all_payments", "ar_aging"}


def test_statistics_promote_with_distinct_occupancy_codes(seeded, tmp_path):
    _ingest(seeded, tmp_path, PDF)
    facts = {
        (f.metric_code, f.period, f.is_prior_year): Decimal(str(f.value))
        for f in seeded.scalars(
            select(UsaliStatisticFact).where(UsaliStatisticFact.property_id == "HKDEMO")
        )
    }
    assert facts[("TOTAL_ROOMS", "DAY", False)] == Decimal("80")
    assert facts[("ROOMS_OCCUPIED", "DAY", False)] == Decimal("40")
    assert facts[("ROOM_REVENUE", "DAY", False)] == Decimal("5000.00")
    assert facts[("ADR", "MTD", True)] == Decimal("124.00")
    occ = {code: facts[(code, "DAY", False)] for code in (
        "OCCUPANCY_PCT", "OCCUPANCY_PCT_EX_OOO_EX_COMP",
        "OCCUPANCY_PCT_EX_COMP", "OCCUPANCY_PCT_EX_OOO",
    )}
    assert len(set(occ.values())) == 4, occ


def test_hotelkey_never_produces_financial_facts_or_journal_entries(seeded, tmp_path):
    # GL is ON for this test: a chart and a fiscal calendar are seeded so
    # post_and_record reaches build_plan and a transforming handler WOULD post.
    # Without them it returns "skipped" before looking at any facts, and the
    # journal claim in this test's name would hold vacuously.
    gl_chart.seed_chart(seeded, org_id=1)
    _seed_calendar(seeded, "HKDEMO")
    seeded.commit()
    for p in (PDF, FIX / "Settlement By Payment Type.xlsx", FIX / "All Payments.xlsx"):
        _ingest(seeded, tmp_path, p)
    staged = seeded.scalar(
        select(func.count()).select_from(PmsDailyFinancialStage)
        .where(PmsDailyFinancialStage.pms_source == "HOTELKEY")
    )
    # Fixture counts: 16 financial lines in the statistics PDF (Room Revenue 2,
    # Misc Revenue 5, Taxes 4, Payments 5), 5 settlement detail rows, 2 payment
    # types -- scripts/gen_hotelkey_mock_fixtures.py::STATISTICS_PAGES,
    # build_settlement, build_all_payments.
    assert staged == 16 + 5 + 2
    assert seeded.scalar(
        select(func.count()).select_from(UsaliFinancialFact)
        .where(UsaliFinancialFact.pms_source == "HOTELKEY")
    ) == 0
    assert seeded.scalar(
        select(func.count()).select_from(MappingException)
        .where(MappingException.pms_source == "HOTELKEY")
    ) == 0
    assert seeded.scalar(select(func.count()).select_from(JournalEntry)) == 0
    # The outcome was "skipped" with no facts: neither "posted" nor "failed",
    # both of which leave a gl_posting_ledger row for the property-day.
    ledger_rows = seeded.scalars(
        select(GlPostingLedger).where(GlPostingLedger.property_id == "HKDEMO")
    ).all()
    assert ledger_rows == [], [(r.status, r.message) for r in ledger_rows]


def test_the_transform_cli_cannot_promote_hotelkey_rows(seeded, tmp_path):
    """The handlers never call transform(); this closes the other door. `usali
    transform --source HOTELKEY` calls transform() directly, and without a gate
    it would promote the staged rows and the next upload's gl_posting would
    post them. Same GL-ON setup as the journal test, so a promoted row WOULD
    have had a chart to post against."""
    gl_chart.seed_chart(seeded, org_id=1)
    _seed_calendar(seeded, "HKDEMO")
    seeded.commit()
    _ingest(seeded, tmp_path, PDF)
    with pytest.raises(ValueError, match=r"HOTELKEY .*D-OH22\.6.*never transformed"):
        transform(seeded, source="HOTELKEY", business_date=BD, edition=12)
    seeded.rollback()
    assert seeded.scalar(
        select(func.count()).select_from(UsaliFinancialFact)
        .where(UsaliFinancialFact.pms_source == "HOTELKEY")
    ) == 0
    assert seeded.scalar(select(func.count()).select_from(JournalEntry)) == 0


def test_hotelkey_statistics_file_opens_exactly_one_batch(seeded, tmp_path):
    _ingest(seeded, tmp_path, PDF)
    batches = seeded.scalars(select(IngestBatch)).all()
    assert len(batches) == 1
    assert (batches[0].pms_source, batches[0].report_type, batches[0].status) == (
        "HOTELKEY", "hotel_statistics", "transformed")
    statistic_rows = seeded.scalar(select(func.count()).select_from(PmsDailyStatisticStage))
    financial_rows = seeded.scalar(select(func.count()).select_from(PmsDailyFinancialStage))
    assert batches[0].row_count == statistic_rows + financial_rows


def test_ar_balances_promote_from_the_transaction_type_totals(seeded, tmp_path):
    _ingest(seeded, tmp_path, FIX / "AR Invoice Aging.xlsx")
    by = {
        f.ledger_code: Decimal(str(f.amount))
        for f in seeded.scalars(
            select(UsaliLedgerBalanceFact).where(UsaliLedgerBalanceFact.property_id == "HKDEMO")
        )
    }
    assert by["AR_LEDGER"] == Decimal("3400.00")
    assert by["AR_AGING_OVER_150"] == Decimal("-100.00")
    assert by["AR_AGING_31_60"] == Decimal("2000.00")


def test_the_coverage_worklist_sees_hotelkey_codes(seeded, tmp_path):
    from usali.reporting import coverage_report

    _ingest(seeded, tmp_path, PDF)
    report = coverage_report(seeded)
    hk = next(s for s in report.sources if s.pms_source == "HOTELKEY")
    assert "DIFFERENCE DROP" in hk.financial.missing_codes
    assert any(e.code == "Taxable Room Revenue" for e in hk.financial.needs_review)


def test_an_unknown_format_is_quarantined_loudly(seeded, tmp_path):
    bogus = tmp_path / "report.xlsx"
    bogus.write_bytes(b"hello, not a workbook")
    with pytest.raises(ProcessingError, match="PDF or XLSX"):
        process_file(seeded, bogus, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail")
    assert (tmp_path / "fail" / "report.xlsx").exists()


def test_reingest_is_idempotent(seeded, tmp_path):
    _ingest(seeded, tmp_path, FIX / "Settlement By Payment Type.xlsx")
    again = tmp_path / "again"
    again.mkdir()
    _ingest(seeded, again, FIX / "Settlement By Payment Type.xlsx")
    assert seeded.scalar(
        select(func.count()).select_from(PmsDailyFinancialStage)
        .where(PmsDailyFinancialStage.pms_source == "HOTELKEY")
    ) == 5
    # One IngestBatch per process_file call is the contract; row idempotency is the
    # row_hash's (file_hash + ordinal), not the batch's.
    assert seeded.scalar(select(func.count()).select_from(IngestBatch)) == 2


_TENANT_TABLES = (
    IngestBatch,
    PmsDailyFinancialStage,
    PmsDailyStatisticStage,
    PmsLedgerBalanceStage,
    UsaliStatisticFact,
    UsaliLedgerBalanceFact,
    IngestionCoverage,
)


def test_hotelkey_ingest_under_the_app_role_stays_inside_its_org(
    db_session, two_tenant_world, app_role_engine, tmp_path
):
    """The whole HotelKey path on an org-2-bound `usali_app` session: a
    signup-created property, all four exports through process_file, and every
    row it writes carries org 2. Org 1 holds the seeded HKDEMO under the SAME
    match phrase, so detection under org 2 has a decoy to get wrong; an
    org-1-bound session then sees none of org 2's rows."""
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/hotelkey.yaml")
    seed_properties(db_session, "mapping/properties.yaml")  # HKDEMO, org 1
    db_session.commit()
    org2 = two_tenant_world.org2_id
    factory = make_session_factory(app_role_engine)

    with factory() as s:
        bind_org_context(s, org2)
        pid = create_first_property(s, org2, name="Lakeside Test Lodge", pms_source="hotelkey")
        s.commit()
        for src in (
            PDF,
            FIX / "Settlement By Payment Type.xlsx",
            FIX / "All Payments.xlsx",
            FIX / "AR Invoice Aging.xlsx",
        ):
            assert _ingest(s, tmp_path, src).property_id == pid

    with factory() as s:
        bind_org_context(s, org2)
        for table in _TENANT_TABLES:
            rows = s.scalars(select(table)).all()
            assert rows, table.__tablename__
            assert {r.org_id for r in rows} == {org2}, table.__tablename__
            if hasattr(table, "property_id"):
                assert {r.property_id for r in rows} == {pid}, table.__tablename__

    with factory() as s:
        bind_org_context(s, 1)
        for table in _TENANT_TABLES:
            assert s.scalar(select(func.count()).select_from(table)) == 0, table.__tablename__
