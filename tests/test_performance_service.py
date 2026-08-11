from datetime import date
from decimal import Decimal

import pytest

from usali.models import (
    IngestBatch,
    Organization,
    PmsDailyStatisticStage,
    Property,
    RoomInventory,
    UsaliSegmentFact,
    UsaliStatisticFact,
)
from usali.performance import (
    AdrBasisUnavailable,
    CoreMetrics,
    _stat_by_day,
    adr_rooms_sold,
    core_metrics,
)


def _prop(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def _stat(session, pid, d, code, value):
    # The fact's FKs (ingest_batch_id, stat_stage_id) are NOT NULL, and
    # stat_stage_id is uniquely constrained, so each fact needs its own staged
    # provenance row for the insert to satisfy the schema.
    batch = IngestBatch(pms_source="OPERA", report_type="manager_flash",
                        source_file="t", file_hash="h", status="staged",
                        row_count=1, error_count=0)
    session.add(batch)
    session.flush()
    stage = PmsDailyStatisticStage(
        property_id=pid, pms_source="OPERA", report_type="manager_flash",
        business_date=d, metric_label=code, period_label="DAY",
        is_prior_year=False, value=value, source_file="t",
        ingest_batch_id=batch.batch_id, row_hash=f"{code}-{d}",
    )
    session.add(stage)
    session.flush()
    return UsaliStatisticFact(property_id=pid, pms_source="OPERA", business_date=d,
                              metric_code=code, period="DAY", is_prior_year=False,
                              value=value, ingest_batch_id=batch.batch_id,
                              stat_stage_id=stage.stat_stage_id)


def _seg(session, pid, d, seg, rooms):
    batch = IngestBatch(pms_source="OPERA", report_type="market_stats", source_file="t",
                        file_hash="hseg", status="staged", row_count=1, error_count=0)
    session.add(batch)
    session.flush()
    return UsaliSegmentFact(property_id=pid, pms_source="OPERA", business_date=d,
                            usali_segment=seg, period="DAY", rooms=rooms, room_revenue=0,
                            ingest_batch_id=batch.batch_id)


def test_stat_by_day_returns_daily_values(db_session):
    _prop(db_session)
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 10000),
        _stat(db_session, "HISJ", date(2026, 1, 2), "ROOM_REVENUE", 12000),
    ])
    db_session.commit()
    got = _stat_by_day(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2), "ROOM_REVENUE")
    assert got == {date(2026, 1, 1): Decimal("10000"), date(2026, 1, 2): Decimal("12000")}


def test_adr_rooms_sold_as_reported_ignores_segments(db_session):
    _prop(db_session)
    db_session.add(_stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100))
    db_session.commit()
    sold = adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "as_reported")
    assert sold == Decimal("100")


def test_adr_rooms_sold_excludes_comp_and_house(db_session):
    _prop(db_session)
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100),
        _seg(db_session, "HISJ", date(2026, 1, 1), "COMPLIMENTARY", 3),
        _seg(db_session, "HISJ", date(2026, 1, 1), "HOUSE_USE", 2),
        _seg(db_session, "HISJ", date(2026, 1, 1), "TRANSIENT", 95),
    ])
    db_session.commit()
    sold = adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "exclude_comp_house")
    assert sold == Decimal("95")  # 100 - 3 - 2


def test_adr_rooms_sold_refuses_exclude_without_segments(db_session):
    _prop(db_session)
    db_session.add(_stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100))
    db_session.commit()  # no segment rows for the day
    with pytest.raises(AdrBasisUnavailable):
        adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "exclude_comp_house")


def test_core_metrics_and_revpar_crosscheck(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat(db_session, "HISJ", date(2026, 1, 1), "TOTAL_REVENUE", 18000),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    assert isinstance(m, CoreMetrics)
    assert m.rooms_available == Decimal("100")
    assert m.rooms_sold == Decimal("80")
    assert m.occupancy == Decimal("0.8000")
    assert m.adr == Decimal("150.0000")
    assert m.revpar == Decimal("120.0000")
    assert m.trevpar == Decimal("180.0000")
    assert abs(m.adr * m.occupancy - m.revpar) <= Decimal("0.01")


@pytest.mark.skip(reason="InventoryInconsistent lands with the PR #25 rebase")
def test_core_metrics_refuses_negative_denominator(db_session):
    from usali.inventory import InventoryInconsistent
    from usali.models import OutOfOrderRoom
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=10))
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 1),
                                  end_date=date(2026, 1, 1), room_count=25, reason_code="do_not_rent"))
    db_session.add(_stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 5))
    db_session.commit()
    with pytest.raises(InventoryInconsistent):
        core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
