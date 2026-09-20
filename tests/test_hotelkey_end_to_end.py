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

from usali.ingestion import ProcessingError, process_file
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import (
    IngestBatch,
    IngestionCoverage,
    JournalEntry,
    MappingException,
    PmsDailyFinancialStage,
    PmsDailyStatisticStage,
    UsaliFinancialFact,
    UsaliLedgerBalanceFact,
    UsaliStatisticFact,
)

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
