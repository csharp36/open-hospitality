from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from usali.models import PmsDailySegmentStage
from usali.schemas import SegmentRecord
from usali.segment_stage import stage_segments


@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def _rec(code: str, measure: str, value: str, period: str = "DAY") -> SegmentRecord:
    return SegmentRecord(
        property_id="HISJ",
        pms_source="OPERA",
        report_type="market_stats",
        business_date=date(2026, 7, 7),
        segment_code=code,
        segment_desc="Discount - D",
        measure=measure,
        period_label=period,
        value=Decimal(value),
    )


def test_stage_segments_inserts_and_batches(db_session):
    batch = stage_segments(
        db_session, [_rec("D", "ROOMS", "35")], source_file="ms.pdf", file_hash="g1"
    )
    db_session.commit()
    assert isinstance(batch.batch_id, int)
    assert db_session.scalar(select(func.count()).select_from(PmsDailySegmentStage)) == 1


def test_stage_segments_is_idempotent(db_session):
    recs = [_rec("D", "ROOMS", "35"), _rec("D", "ROOM_REVENUE", "6047.33")]
    stage_segments(db_session, recs, source_file="ms.pdf", file_hash="dup")
    db_session.commit()
    stage_segments(db_session, recs, source_file="ms.pdf", file_hash="dup")
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(PmsDailySegmentStage)) == 2


def test_identical_rows_in_one_report_both_survive(db_session):
    recs = [_rec("D", "ROOMS", "1"), _rec("D", "ROOMS", "1")]
    stage_segments(db_session, recs, source_file="ms.pdf", file_hash="same")
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(PmsDailySegmentStage)) == 2
