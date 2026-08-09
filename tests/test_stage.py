from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from usali.models import IngestBatch, PmsDailyFinancialStage
from usali.schemas import StagedRecord
from usali.stage import stage_records


@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def _rec(code: str, amt: str) -> StagedRecord:
    return StagedRecord(
        property_id="HISJ",
        pms_source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pms_trx_code=code,
        pms_trx_desc="desc",
        raw_amount=Decimal(amt),
        section="Revenue",
    )


def test_stage_inserts_rows_and_batch(db_session):
    batch = stage_records(db_session, [_rec("1000", "10395.00")], source_file="tb.pdf", file_hash="h1")
    db_session.commit()
    assert isinstance(batch.batch_id, int)
    count = db_session.scalar(select(func.count()).select_from(PmsDailyFinancialStage))
    assert count >= 1


def test_stage_is_idempotent_on_rehash(db_session):
    recs = [_rec("2000", "5.00"), _rec("2001", "6.00")]
    stage_records(db_session, recs, source_file="tb.pdf", file_hash="dup")
    db_session.commit()
    before = db_session.scalar(select(func.count()).select_from(PmsDailyFinancialStage))
    stage_records(db_session, recs, source_file="tb.pdf", file_hash="dup")
    db_session.commit()
    after = db_session.scalar(select(func.count()).select_from(PmsDailyFinancialStage))
    assert after == before


def test_stage_keeps_distinct_rows_with_identical_values(db_session):
    # Two genuinely distinct lines sharing the same (code, amount, section) must BOTH be
    # staged (no silent drop). The row's ordinal position disambiguates them in the hash.
    recs = [_rec("1000", "100.00"), _rec("1000", "100.00")]
    stage_records(db_session, recs, source_file="tb.pdf", file_hash="dupvals")
    db_session.commit()
    count = db_session.scalar(select(func.count()).select_from(PmsDailyFinancialStage))
    assert count == 2

    # Re-ingesting the same file is still idempotent (same order -> same hashes).
    stage_records(db_session, recs, source_file="tb.pdf", file_hash="dupvals")
    db_session.commit()
    after = db_session.scalar(select(func.count()).select_from(PmsDailyFinancialStage))
    assert after == 2
