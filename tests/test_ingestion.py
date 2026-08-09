import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from usali.ingestion import ProcessingError, process_file
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import IngestBatch, UsaliFinancialFact

OPERA_PDF = Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf")


def _seed(db_session) -> None:
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    load_mappings(db_session, "mapping/autoclerk.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()


def test_process_file_full_pipeline(db_session, tmp_path):
    _seed(db_session)
    inbox, processed, failed = tmp_path / "inbox", tmp_path / "processed", tmp_path / "failed"
    inbox.mkdir()
    pdf = inbox / OPERA_PDF.name
    shutil.copy(OPERA_PDF, pdf)

    result = process_file(
        db_session, pdf, processed_dir=processed, failed_dir=failed
    )

    assert result.pms_source == "OPERA"
    assert result.property_id == "HISJ"
    assert result.staged == 14
    assert result.mapped == 14 and result.unmapped == 0
    assert not pdf.exists()
    assert (processed / pdf.name).exists()

    total = db_session.scalar(select(func.sum(UsaliFinancialFact.amount)))
    assert total is not None

    batch = db_session.execute(select(IngestBatch)).scalars().one()
    assert batch.status == "transformed"


def test_process_file_reprocessing_same_file_is_noop(db_session, tmp_path):
    _seed(db_session)
    inbox, processed, failed = tmp_path / "inbox", tmp_path / "processed", tmp_path / "failed"
    inbox.mkdir()
    pdf = inbox / OPERA_PDF.name
    shutil.copy(OPERA_PDF, pdf)
    process_file(db_session, pdf, processed_dir=processed, failed_dir=failed)

    # Same file dropped again: stage dedupes, transform skips — no double counting.
    pdf2 = inbox / OPERA_PDF.name
    shutil.copy(OPERA_PDF, pdf2)
    result = process_file(db_session, pdf2, processed_dir=processed, failed_dir=failed)
    assert result.mapped == 0 and result.skipped == 14

    count = db_session.scalar(select(func.count()).select_from(UsaliFinancialFact))
    assert count == 14


def test_process_file_failure_quarantines(db_session, tmp_path):
    _seed(db_session)
    inbox, processed, failed = tmp_path / "inbox", tmp_path / "processed", tmp_path / "failed"
    inbox.mkdir()
    bad = inbox / "not-a-report.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")

    with pytest.raises(ProcessingError):
        process_file(db_session, bad, processed_dir=processed, failed_dir=failed)

    assert not bad.exists()
    assert (failed / bad.name).exists()
    batch = db_session.execute(select(IngestBatch)).scalars().one()
    assert batch.status == "failed"
    assert batch.message


def test_filing_failure_after_commit_does_not_fabricate_failed_batch(
    db_session, tmp_path, monkeypatch
):
    # A move failure AFTER the success commit must not roll anything back, must not
    # record a second (failed) batch, and must leave the file in place for retry.
    import usali.ingestion as ingestion

    _seed(db_session)
    inbox, processed, failed = tmp_path / "inbox", tmp_path / "processed", tmp_path / "failed"
    inbox.mkdir()
    pdf = inbox / OPERA_PDF.name
    shutil.copy(OPERA_PDF, pdf)

    real_move = ingestion._move

    def move_fails_on_processed(path, target_dir):
        if target_dir == processed:
            raise OSError("simulated permission error")
        return real_move(path, target_dir)

    monkeypatch.setattr(ingestion, "_move", move_fails_on_processed)

    with pytest.raises(ProcessingError, match="data committed"):
        process_file(db_session, pdf, processed_dir=processed, failed_dir=failed)

    assert pdf.exists()  # file stays in the inbox for retry
    assert not (failed / pdf.name).exists()
    batch = db_session.execute(select(IngestBatch)).scalars().one()  # exactly ONE batch
    assert batch.status == "transformed"
    count = db_session.scalar(select(func.count()).select_from(UsaliFinancialFact))
    assert count == 14  # committed data untouched

    # Retry with the move restored: idempotent no-op that completes the filing.
    monkeypatch.setattr(ingestion, "_move", real_move)
    result = process_file(db_session, pdf, processed_dir=processed, failed_dir=failed)
    assert result.skipped == 14
    assert (processed / pdf.name).exists()


def test_process_file_statistics_report(db_session, tmp_path):
    _seed(db_session)
    inbox, processed, failed = tmp_path / "inbox", tmp_path / "processed", tmp_path / "failed"
    inbox.mkdir()
    src = Path("docs/reference/samples/Manager Flash 07.07.2026 - Opera.pdf")
    pdf = inbox / src.name
    shutil.copy(src, pdf)

    result = process_file(db_session, pdf, processed_dir=processed, failed_dir=failed)

    assert result.report_type == "manager_flash"
    assert result.staged > 0
    assert result.mapped > 0  # promoted canonical metrics
    assert (processed / pdf.name).exists()

    from sqlalchemy import select as sa_select

    from usali.models import UsaliStatisticFact
    facts = db_session.execute(sa_select(UsaliStatisticFact)).scalars().all()
    adr = [f for f in facts if f.metric_code == "ADR" and f.period == "DAY" and not f.is_prior_year]
    assert len(adr) == 1 and adr[0].value == Decimal("167.66")


def test_process_file_segmentation_reports(db_session, tmp_path):
    _seed(db_session)
    inbox, processed, failed = tmp_path / "inbox", tmp_path / "processed", tmp_path / "failed"
    inbox.mkdir()
    for name in (
        "Market Code Statistics 07.07.2026 - Opera.pdf",
        "Autoclerk - Revenue by Rate Plan 07.07.2026.pdf",
    ):
        shutil.copy(Path("docs/reference/samples") / name, inbox / name)

    results = [
        process_file(db_session, pdf, processed_dir=processed, failed_dir=failed)
        for pdf in sorted(inbox.glob("*.pdf"))
    ]
    by_report = {r.report_type: r for r in results}
    assert by_report["market_stats"].mapped == 15   # 5 segments x 3 periods
    assert by_report["rate_plan"].mapped == 3       # TRANSIENT/CONTRACT/COMPLIMENTARY, DAY

    from sqlalchemy import select as sa_select

    from usali.models import UsaliSegmentFact
    facts = db_session.execute(sa_select(UsaliSegmentFact)).scalars().all()
    by_key = {(f.pms_source, f.usali_segment, f.period): (f.rooms, f.room_revenue) for f in facts}
    assert by_key[("AUTOCLERK", "CONTRACT", "DAY")] == (Decimal("1"), Decimal("75.00"))
    opera_day = [(k, v) for k, v in by_key.items() if k[0] == "OPERA" and k[2] == "DAY"]
    assert sum((v[1] for _, v in opera_day), Decimal("0")) == Decimal("10395.00")
