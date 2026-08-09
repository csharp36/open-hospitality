from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from usali.models import UsaliSegmentFact
from usali.schemas import SegmentRecord
from usali.segment_promote import (
    SegmentMappingError,
    SegmentReconciliationError,
    promote_segments,
)
from usali.segment_stage import stage_segments



@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def _rec(code: str, measure: str, value: str, period: str = "DAY", prop: str = "HISJ") -> SegmentRecord:
    return SegmentRecord(
        property_id=prop, pms_source="OPERA", report_type="market_stats",
        business_date=date(2026, 7, 7), segment_code=code, segment_desc=None,
        measure=measure, period_label=period, value=Decimal(value),
    )


def _stage(db_session, recs, fh: str) -> None:
    stage_segments(db_session, recs, source_file="ms.pdf", file_hash=fh)
    db_session.commit()


def test_promotes_aggregated_segments(db_session):
    # D and G both map to TRANSIENT -> sums aggregate; Z maps to GROUP.
    _stage(db_session, [
        _rec("D", "ROOMS", "35"), _rec("D", "ROOM_REVENUE", "6047.33"),
        _rec("G", "ROOMS", "14"), _rec("G", "ROOM_REVENUE", "2516.95"),
        _rec("Z", "ROOMS", "0"), _rec("Z", "ROOM_REVENUE", "0.00"),
        _rec("D", "ADR", "172.78"),  # non-core measure: stage-only, ignored by the fact
        _rec("TOTAL", "ROOMS", "49"), _rec("TOTAL", "ROOM_REVENUE", "8564.28"),
    ], "sp1")
    result = promote_segments(
        db_session, "mapping/segments.yaml", source="OPERA", business_date=date(2026, 7, 7)
    )
    db_session.commit()
    assert result.promoted_segments == 2  # TRANSIENT + GROUP (DAY only)
    facts = db_session.execute(select(UsaliSegmentFact)).scalars().all()
    by_key = {(f.usali_segment, f.period): (f.rooms, f.room_revenue) for f in facts}
    assert by_key[("TRANSIENT", "DAY")] == (Decimal("49"), Decimal("8564.28"))
    assert by_key[("GROUP", "DAY")] == (Decimal("0"), Decimal("0.00"))


def test_unmapped_code_raises(db_session):
    _stage(db_session, [
        _rec("ZZ9", "ROOMS", "1"), _rec("ZZ9", "ROOM_REVENUE", "100.00"),
        _rec("TOTAL", "ROOMS", "1"), _rec("TOTAL", "ROOM_REVENUE", "100.00"),
    ], "sp2")
    with pytest.raises(SegmentMappingError, match="ZZ9"):
        promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                         business_date=date(2026, 7, 7))


def test_reconciliation_mismatch_raises(db_session):
    _stage(db_session, [
        _rec("D", "ROOMS", "35"), _rec("D", "ROOM_REVENUE", "6047.33"),
        _rec("TOTAL", "ROOMS", "62"), _rec("TOTAL", "ROOM_REVENUE", "10395.00"),
    ], "sp3")
    with pytest.raises(SegmentReconciliationError, match="DAY"):
        promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                         business_date=date(2026, 7, 7))


def test_missing_total_raises(db_session):
    _stage(db_session, [
        _rec("D", "ROOMS", "35"), _rec("D", "ROOM_REVENUE", "6047.33"),
    ], "sp4")
    with pytest.raises(SegmentReconciliationError, match="TOTAL"):
        promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                         business_date=date(2026, 7, 7))


def test_repromotion_is_skip_noop(db_session):
    _stage(db_session, [
        _rec("D", "ROOMS", "35"), _rec("D", "ROOM_REVENUE", "6047.33"),
        _rec("TOTAL", "ROOMS", "35"), _rec("TOTAL", "ROOM_REVENUE", "6047.33"),
    ], "sp5")
    promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                     business_date=date(2026, 7, 7))
    db_session.commit()
    second = promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                              business_date=date(2026, 7, 7))
    db_session.commit()
    assert second.promoted_segments == 0 and second.skipped > 0
    facts = db_session.execute(select(UsaliSegmentFact)).scalars().all()
    assert len(facts) == 1


def test_canonical_periods(db_session):
    _stage(db_session, [
        _rec("D", "ROOMS", "138", "MONTH"), _rec("D", "ROOM_REVENUE", "23750.35", "MONTH"),
        _rec("TOTAL", "ROOMS", "138", "MONTH"), _rec("TOTAL", "ROOM_REVENUE", "23750.35", "MONTH"),
    ], "sp6")
    promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                     business_date=date(2026, 7, 7))
    db_session.commit()
    facts = db_session.execute(select(UsaliSegmentFact)).scalars().all()
    assert {f.period for f in facts} == {"MTD"}


def test_two_properties_same_source_date_promote_independently(db_session):
    _stage(db_session, [
        _rec("D", "ROOMS", "35"), _rec("D", "ROOM_REVENUE", "6047.33"),
        _rec("TOTAL", "ROOMS", "35"), _rec("TOTAL", "ROOM_REVENUE", "6047.33"),
        _rec("Z", "ROOMS", "10", prop="OPRB"), _rec("Z", "ROOM_REVENUE", "1500.00", prop="OPRB"),
        _rec("TOTAL", "ROOMS", "10", prop="OPRB"), _rec("TOTAL", "ROOM_REVENUE", "1500.00", prop="OPRB"),
    ], "mp1")
    result = promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                              business_date=date(2026, 7, 7))
    db_session.commit()
    assert result.promoted_segments == 2
    facts = db_session.execute(select(UsaliSegmentFact)).scalars().all()
    by_key = {(f.property_id, f.usali_segment): (f.rooms, f.room_revenue) for f in facts}
    assert by_key[("HISJ", "TRANSIENT")] == (Decimal("35"), Decimal("6047.33"))
    assert by_key[("OPRB", "GROUP")] == (Decimal("10"), Decimal("1500.00"))


def test_second_property_promotes_after_first_already_done(db_session):
    _stage(db_session, [
        _rec("D", "ROOMS", "35"), _rec("D", "ROOM_REVENUE", "6047.33"),
        _rec("TOTAL", "ROOMS", "35"), _rec("TOTAL", "ROOM_REVENUE", "6047.33"),
    ], "mp2a")
    promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                     business_date=date(2026, 7, 7))
    db_session.commit()
    # Property B's report arrives later for the SAME source and date.
    _stage(db_session, [
        _rec("Z", "ROOMS", "10", prop="OPRB"), _rec("Z", "ROOM_REVENUE", "1500.00", prop="OPRB"),
        _rec("TOTAL", "ROOMS", "10", prop="OPRB"), _rec("TOTAL", "ROOM_REVENUE", "1500.00", prop="OPRB"),
    ], "mp2b")
    result = promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                              business_date=date(2026, 7, 7))
    db_session.commit()
    assert result.promoted_segments == 1  # B promoted; A's rows skipped, NOT silently dropped
    facts = db_session.execute(select(UsaliSegmentFact)).scalars().all()
    assert {(f.property_id, f.usali_segment) for f in facts} == {
        ("HISJ", "TRANSIENT"), ("OPRB", "GROUP"),
    }


def test_ignored_counter_distinguishes_nonqualifying_rows(db_session):
    _stage(db_session, [
        _rec("D", "ADR", "172.78"),           # non-core measure
        _rec("D", "ROOMS", "1", "WEEK"),      # non-canonical period
    ], "mp3")
    result = promote_segments(db_session, "mapping/segments.yaml", source="OPERA",
                              business_date=date(2026, 7, 7))
    assert result.promoted_segments == 0
    assert result.ignored == 2
    assert result.skipped == 0
