from datetime import date
from decimal import Decimal

from usali.models import (
    IngestBatch,
    IngestionCoverage,
    Organization,
    PmsDailyStatisticStage,
    Property,
    RoomInventory,
    UsaliSegmentFact,
    UsaliStatisticFact,
)
from usali.performance import RollingStat, TrendPair, Trends, trends


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


def _seed_day(session, pid, d, *, rooms, room_rev, total_rev, complete=True):
    """One business date: the three core DAY statistics plus (when complete) its
    IngestionCoverage manager_flash row. An incomplete day gets its facts but NO
    coverage row, so `complete_days` — hence every trend base — must skip it."""
    session.add_all([
        _stat(session, pid, d, "ROOMS_OCCUPIED", rooms),
        _stat(session, pid, d, "ROOM_REVENUE", room_rev),
        _stat(session, pid, d, "TOTAL_REVENUE", total_rev),
    ])
    if complete:
        session.add(IngestionCoverage(property_id=pid, business_date=d,
                                      report_type="manager_flash"))


# Occupancy is rooms sold / 100 available. 0.70 == rooms 70; ADR held at 100 so
# room_rev == rooms * 100; total_rev is arbitrary-but-consistent.
def _occ70(session, pid, d, *, complete=True):
    _seed_day(session, pid, d, rooms=70, room_rev=7000, total_rev=10000, complete=complete)


def test_wow_excludes_incomplete_days(db_session):
    # Jan 1..14 all occ 0.70 and COMPLETE, EXCEPT Jan 10 which is INCOMPLETE (no
    # coverage row) and carries a wild outlier. An uncovered day must not move any
    # average: both WoW legs stay exactly 0.7000.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    for day in range(1, 15):
        d = date(2026, 1, day)
        if day == 10:
            # outlier value, but NO coverage -> excluded from every trend base
            _seed_day(db_session, "HISJ", d, rooms=99, room_rev=99000, total_rev=200000, complete=False)
        else:
            _occ70(db_session, "HISJ", d)
    db_session.commit()

    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="as_reported")
    assert isinstance(t, Trends)
    assert isinstance(t.wow["occupancy"], TrendPair)
    # current == mean(Jan 8..14 minus Jan 10) == 0.7000; prior == mean(Jan 1..7) == 0.7000
    assert t.wow["occupancy"].current == Decimal("0.7000")
    assert t.wow["occupancy"].prior == Decimal("0.7000")
    # equal legs -> zero variance (defined, signed)
    assert t.wow["occupancy"].delta_pct == Decimal("0.0")


def test_mtd_is_mean_over_complete_month_to_date(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    for day in range(1, 15):
        d = date(2026, 1, day)
        if day == 10:
            _seed_day(db_session, "HISJ", d, rooms=99, room_rev=99000, total_rev=200000, complete=False)
        else:
            _occ70(db_session, "HISJ", d)
    db_session.commit()

    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="as_reported")
    # month-to-date == Jan 1..14 minus the incomplete Jan 10, all 0.70 -> 0.7000
    assert t.mtd["occupancy"] == Decimal("0.7000")


def test_rolling_30_avg_stdev_and_n(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    for day in range(1, 15):
        d = date(2026, 1, day)
        if day == 10:
            _seed_day(db_session, "HISJ", d, rooms=99, room_rev=99000, total_rev=200000, complete=False)
        else:
            _occ70(db_session, "HISJ", d)
    db_session.commit()

    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="as_reported")
    r = t.rolling_30["occupancy"]
    assert isinstance(r, RollingStat)
    # 30-day window Dec 16..Jan 14: only Jan 1..14 exist, minus incomplete Jan 10 == 13 days
    assert r.n == 13
    assert r.avg == Decimal("0.7000")
    # all-equal values -> population stdev is exactly zero
    assert r.stdev == Decimal("0.0000")


def test_day_of_week_current_vs_same_weekday_prior_week(db_session):
    # Anchor Jan 14 (Wed) occ 0.80; anchor-7 Jan 7 (Wed) occ 0.60; both complete.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    for day in range(1, 15):
        d = date(2026, 1, day)
        if day == 14:
            _seed_day(db_session, "HISJ", d, rooms=80, room_rev=8000, total_rev=12000)
        elif day == 7:
            _seed_day(db_session, "HISJ", d, rooms=60, room_rev=6000, total_rev=9000)
        else:
            _occ70(db_session, "HISJ", d)
    db_session.commit()

    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="as_reported")
    assert t.dow["occupancy"].current == Decimal("0.8000")
    assert t.dow["occupancy"].prior == Decimal("0.6000")
    # 0.80 vs 0.60 == +33.3%
    assert t.dow["occupancy"].delta_pct is not None
    assert t.dow["occupancy"].delta_pct == Decimal("33.3")


def test_dow_prior_none_when_prior_week_incomplete(db_session):
    # If the same weekday one week back is data-incomplete, the prior leg is None
    # (no coverage -> not in the series) and the delta degrades to None.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    for day in range(1, 15):
        d = date(2026, 1, day)
        if day == 7:
            _seed_day(db_session, "HISJ", d, rooms=60, room_rev=6000, total_rev=9000, complete=False)
        elif day == 14:
            _seed_day(db_session, "HISJ", d, rooms=80, room_rev=8000, total_rev=12000)
        else:
            _occ70(db_session, "HISJ", d)
    db_session.commit()

    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="as_reported")
    assert t.dow["occupancy"].current == Decimal("0.8000")
    assert t.dow["occupancy"].prior is None
    assert t.dow["occupancy"].delta_pct is None


def test_trends_degrade_adr_on_segmentless_trailing_day_under_exclude_comp_house(db_session):
    # exclude_comp_house property. The REQUESTED day (anchor Jan 14) is fully
    # segment-covered, but an earlier COMPLETE day in the 30-day trend window
    # (Jan 8: stats + coverage + inventory, occupied rooms, NO segments) cannot
    # net comp/house-use from ADR. trends() must NOT raise: occupancy/revpar/
    # trevpar still include Jan 8, only its ADR is dropped from the ADR trend.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    # Jan 14 (anchor): segment-covered so exclude_comp_house resolves.
    _seed_day(db_session, "HISJ", date(2026, 1, 14), rooms=80, room_rev=8000, total_rev=12000)
    db_session.add_all([
        _seg(db_session, "HISJ", date(2026, 1, 14), "COMPLIMENTARY", 3),
        _seg(db_session, "HISJ", date(2026, 1, 14), "HOUSE_USE", 2),
        _seg(db_session, "HISJ", date(2026, 1, 14), "TRANSIENT", 75),
    ])
    # Jan 8: complete + occupied, but NO segment data -> AdrBasisUnavailable per-day.
    _seed_day(db_session, "HISJ", date(2026, 1, 8), rooms=70, room_rev=7000, total_rev=10000)
    db_session.commit()

    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="exclude_comp_house")
    # No raise, and the segmentless day contributes to the basis-independent
    # trends (both days) but not to ADR (only the segment-covered anchor).
    assert t.rolling_30["occupancy"].n == 2
    assert t.rolling_30["revpar"].n == 2
    assert t.rolling_30["trevpar"].n == 2
    assert t.rolling_30["adr"].n == 1
    # The ADR mean is the anchor's alone: 8000 / (80 - 3 - 2) == 106.6667.
    assert t.rolling_30["adr"].avg == Decimal("106.6667")


def test_anchor_is_recorded(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    _occ70(db_session, "HISJ", date(2026, 1, 14))
    db_session.commit()
    t = trends(db_session, "HISJ", date(2026, 1, 14), basis="as_reported")
    assert t.anchor == date(2026, 1, 14)
    assert set(t.wow) == {"occupancy", "adr", "revpar", "trevpar"}
