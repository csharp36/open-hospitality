from datetime import date
from decimal import Decimal

import pytest

from tests.employees import make_employee
from usali.models import (
    IngestBatch,
    IngestionCoverage,
    Organization,
    PmsDailyFinancialStage,
    PmsDailyStatisticStage,
    Property,
    RoomInventory,
    Timecard,
    UsaliFinancialFact,
    UsaliLaborFact,
    UsaliSegmentFact,
    UsaliStatisticFact,
)
from usali.performance import (
    REQUIRED_STATISTICS_REPORTS,
    AdrBasisUnavailable,
    CoreMetrics,
    SegmentInconsistent,
    _ratio,
    _stat_by_day,
    adr_rooms_sold,
    compare,
    complete_days,
    core_metrics,
    labor_productivity,
    reconciliation,
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


def _fin_rooms(session, pid, d, amount, *, row_hash="rooms"):
    """One Rooms financial fact (with its required batch + stage provenance) —
    the drill-through target the room_revenue reconciliation cross-checks."""
    batch = IngestBatch(pms_source="OPERA", report_type="trial_balance",
                        source_file="t.pdf", file_hash="0" * 64, status="staged",
                        row_count=1, error_count=0)
    session.add(batch)
    session.flush()
    stage = PmsDailyFinancialStage(
        property_id=pid, pms_source="OPERA", report_type="trial_balance",
        business_date=d, pms_trx_code="1000", raw_amount=amount,
        source_file="t.pdf", ingest_batch_id=batch.batch_id, row_hash=f"{row_hash}-{d}",
    )
    session.add(stage)
    session.flush()
    return UsaliFinancialFact(
        property_id=pid, pms_source="OPERA", business_date=d, usali_edition=12,
        usali_schedule_id=1, usali_major_category="Operating Revenue",
        usali_sub_category="Rooms", usali_line_item="Transient", amount=amount,
        ingest_batch_id=batch.batch_id, stage_id=stage.stage_id,
    )


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


def test_adr_rooms_sold_refuses_when_comp_house_exceed_occupied(db_session):
    # Bad data: comp+house rooms (110) exceed occupied (100). Netting would yield a
    # NEGATIVE rooms-sold basis and a negative ADR — refuse (adr-010) rather than
    # publish nonsense.
    _prop(db_session)
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100),
        _seg(db_session, "HISJ", date(2026, 1, 1), "COMPLIMENTARY", 60),
        _seg(db_session, "HISJ", date(2026, 1, 1), "HOUSE_USE", 50),
    ])
    db_session.commit()
    with pytest.raises(SegmentInconsistent):
        adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "exclude_comp_house")


def test_adr_rooms_sold_refuses_when_comp_house_equal_occupied(db_session):
    # net == 0 (comp+house == occupied) is just as nonsensical a basis as negative:
    # refuse rather than silently publish a None ADR.
    _prop(db_session)
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100),
        _seg(db_session, "HISJ", date(2026, 1, 1), "COMPLIMENTARY", 60),
        _seg(db_session, "HISJ", date(2026, 1, 1), "HOUSE_USE", 40),
    ])
    db_session.commit()
    with pytest.raises(SegmentInconsistent):
        adr_rooms_sold(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), "exclude_comp_house")


def test_core_metrics_exclude_comp_house_segment_inconsistent_propagates(db_session):
    # The refusal must propagate through core_metrics on the exclude_comp_house basis.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _seg(db_session, "HISJ", date(2026, 1, 1), "COMPLIMENTARY", 60),
        _seg(db_session, "HISJ", date(2026, 1, 1), "HOUSE_USE", 50),
    ])
    db_session.commit()
    with pytest.raises(SegmentInconsistent):
        core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="exclude_comp_house")


def test_ratio_negative_denominator_returns_none():
    # Defense-in-depth: a negative denominator must NEVER produce a published
    # metric (mutation-proof: guards the den < 0 path directly).
    assert _ratio(Decimal("-5"), Decimal("-10")) is None
    assert _ratio(Decimal("5"), Decimal("0")) is None
    assert _ratio(Decimal("5"), Decimal("10")) == Decimal("0.5000")


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


def test_labor_hours_per_occupied_room(db_session):
    # HOURS are pure operational detail — NEVER disclosure-gated — so a bare
    # promoted labor fact (even one with no priced cost) still yields the ratio.
    _prop(db_session)
    db_session.add(_stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80))
    # UsaliLaborFact.timecard_id is NOT NULL with a composite FK onto timecard,
    # so seed a real employee + approved card for the fact to hang off.
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H", pay_type="hourly")
    db_session.flush()
    card = Timecard(employee_id=emp.employee_id, period_start=date(2025, 12, 29),
                    period_end=date(2026, 1, 11), status="approved")
    db_session.add(card)
    db_session.flush()
    db_session.add(UsaliLaborFact(property_id="HISJ", business_date=date(2026, 1, 1),
                                  department_id=None, hours=Decimal("160"), ot_hours=Decimal("0"),
                                  est_cost=Decimal("0"), timecard_id=card.timecard_id))
    db_session.commit()
    p = labor_productivity(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1))
    assert p.labor_hours == Decimal("160")
    assert p.hours_per_occupied_room == Decimal("2.0000")  # 160/80


def test_reconciliation_flags_divergence(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),  # ADR weight
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_AVAILABLE", 100),  # RevPAR weight
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ADR", 150),      # agrees with computed 150
        _stat(db_session, "HISJ", date(2026, 1, 1), "REVPAR", 999),   # diverges from computed 120
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["adr"].agrees is True
    assert recon["revpar"].agrees is False


def test_reconciliation_normalizes_ingested_occupancy_percent(db_session):
    # Ingested OCCUPANCY_PCT is a PERCENTAGE (80.0), computed occupancy a FRACTION
    # (0.8000): the cross-check must normalize before comparing.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_AVAILABLE", 100),  # occupancy weight
        _stat(db_session, "HISJ", date(2026, 1, 1), "OCCUPANCY_PCT", 80),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["occupancy"].computed == Decimal("0.8000")
    assert recon["occupancy"].ingested == Decimal("0.8")
    assert recon["occupancy"].agrees is True


def test_reconciliation_missing_ingested_stat_yields_none(db_session):
    # A MISSING ingested stat must not crash: `agrees` is None (nothing to check).
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["adr"].ingested is None
    assert recon["adr"].agrees is None
    assert recon["revpar"].agrees is None
    assert recon["occupancy"].agrees is None


def test_reconciliation_room_revenue_and_rooms_available_agree(db_session):
    # room_revenue: ROOM_REVENUE statistic (12000) reconciles to the SUM of the
    # Rooms financial-fact line (12000). rooms_available: inventory availability
    # (100) reconciles to the ingested ROOMS_AVAILABLE DAY statistic (100).
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_AVAILABLE", 100),
        _fin_rooms(db_session, "HISJ", date(2026, 1, 1), Decimal("12000")),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["room_revenue"].agrees is True
    assert recon["rooms_available"].agrees is True


def test_reconciliation_room_revenue_and_rooms_available_diverge(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_AVAILABLE", 90),   # != computed 100
        _fin_rooms(db_session, "HISJ", date(2026, 1, 1), Decimal("11000")),   # != computed 12000
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["room_revenue"].agrees is False
    assert recon["rooms_available"].agrees is False


def test_reconciliation_room_revenue_and_rooms_available_absent_yield_none(db_session):
    # No Rooms financial fact and no ROOMS_AVAILABLE statistic: each cross-check has
    # nothing to reconcile against, so `agrees` is None and nothing raises.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["room_revenue"].ingested is None
    assert recon["room_revenue"].agrees is None
    assert recon["rooms_available"].ingested is None
    assert recon["rooms_available"].agrees is None


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


def test_prior_period_and_prior_year(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2025, 1, 1), total_rooms=100))
    # current day 2026-01-08 (occ 0.80); prior-period day 2026-01-07 (occ 0.60).
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOM_REVENUE", 12000),
        _stat(db_session, "HISJ", date(2026, 1, 8), "TOTAL_REVENUE", 18000),
        _stat(db_session, "HISJ", date(2026, 1, 7), "ROOMS_OCCUPIED", 60),
        _stat(db_session, "HISJ", date(2026, 1, 7), "ROOM_REVENUE", 9000),
        _stat(db_session, "HISJ", date(2026, 1, 7), "TOTAL_REVENUE", 15000),
    ])
    db_session.commit()
    cmp = compare(db_session, "HISJ", date(2026, 1, 8), date(2026, 1, 8), basis="as_reported")
    assert cmp.current.occupancy == Decimal("0.8000")
    assert cmp.prior_period.occupancy == Decimal("0.6000")
    assert cmp.prior_period_delta_pct["occupancy"] is not None    # +33.3%
    # no 2025 history for this window -> prior-year None, delta None (no zero-div)
    assert cmp.prior_year is None or cmp.prior_year.occupancy is None
    assert cmp.prior_year_delta_pct["occupancy"] is None


def test_prior_period_delta_is_signed_percentage(db_session):
    # 0.80 vs 0.60 == +33.3%; the delta must be the signed rounded variance.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 7), "ROOMS_OCCUPIED", 60),
    ])
    db_session.commit()
    cmp = compare(db_session, "HISJ", date(2026, 1, 8), date(2026, 1, 8), basis="as_reported")
    assert cmp.prior_period_delta_pct["occupancy"] == Decimal("33.3")


def test_prior_year_works_when_history_exists(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2025, 1, 1), total_rooms=100))
    # current 2026-01-08 (occ 0.80); same window one year earlier 2025-01-08 (occ 0.50).
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOM_REVENUE", 12000),
        _stat(db_session, "HISJ", date(2025, 1, 8), "ROOMS_OCCUPIED", 50),
        _stat(db_session, "HISJ", date(2025, 1, 8), "ROOM_REVENUE", 6000),
    ])
    db_session.commit()
    cmp = compare(db_session, "HISJ", date(2026, 1, 8), date(2026, 1, 8), basis="as_reported")
    assert cmp.prior_year is not None
    assert cmp.prior_year.occupancy == Decimal("0.5000")
    # 0.80 vs 0.50 == +60.0%
    assert cmp.prior_year_delta_pct["occupancy"] == Decimal("60.0")


def test_zero_prior_value_yields_none_delta_no_zero_div(db_session):
    # A prior window WITH landed history but a zero metric (occ 0/100) must give a
    # None delta, never a ZeroDivisionError.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 7), "ROOMS_OCCUPIED", 0),  # landed fact, zero value
    ])
    db_session.commit()
    cmp = compare(db_session, "HISJ", date(2026, 1, 8), date(2026, 1, 8), basis="as_reported")
    assert cmp.prior_period is not None
    assert cmp.prior_period.occupancy == Decimal("0.0000")
    assert cmp.prior_period_delta_pct["occupancy"] is None


def test_prior_year_leap_day_window_falls_back_safely(db_session):
    # A Feb-29 window shifted to a non-leap prior year has no .replace counterpart;
    # the comparison must degrade to None (365-day fallback), never raise ValueError.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2024, 1, 1), total_rooms=100))
    db_session.add(_stat(db_session, "HISJ", date(2024, 2, 29), "ROOMS_OCCUPIED", 80))
    db_session.commit()
    # 2023 (non-leap) has no history in the shifted window -> prior_year None, no raise.
    cmp = compare(db_session, "HISJ", date(2024, 2, 29), date(2024, 2, 29), basis="as_reported")
    assert cmp.current.occupancy == Decimal("0.8000")
    assert cmp.prior_year is None
    assert cmp.prior_year_delta_pct["occupancy"] is None


def test_reconciliation_volume_weights_multiday_ingested(db_session):
    # ANTI-FALSE-ALARM: a multi-day window with an intra-window inventory change.
    # Day1: avail 100, occ 100, ADR 100. Day2: avail 10, occ 0, ADR 200.
    # Computed occupancy = (100+0)/(100+10) = 0.9091 (volume/availability weighted).
    # An UNWEIGHTED mean of ingested OCCUPANCY_PCT (100%, 0%) is 0.50 and would
    # spuriously disagree; the WEIGHTED ingested occupancy is 0.9091 and agrees.
    _prop(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100),
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 2), total_rooms=10),
    ])
    db_session.add_all([
        # Day 1
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 10000),  # ADR 100
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_AVAILABLE", 100),
        _stat(db_session, "HISJ", date(2026, 1, 1), "OCCUPANCY_PCT", 100),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ADR", 100),
        # Day 2 (zero occupancy on 10 available rooms)
        _stat(db_session, "HISJ", date(2026, 1, 2), "ROOMS_OCCUPIED", 0),
        _stat(db_session, "HISJ", date(2026, 1, 2), "ROOM_REVENUE", 0),
        _stat(db_session, "HISJ", date(2026, 1, 2), "ROOMS_AVAILABLE", 10),
        _stat(db_session, "HISJ", date(2026, 1, 2), "OCCUPANCY_PCT", 0),
        _stat(db_session, "HISJ", date(2026, 1, 2), "ADR", 200),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2), basis="as_reported")
    assert m.occupancy == Decimal("0.9091")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2), m)
    assert recon["occupancy"].agrees is True   # weighted ingested matches (was False unweighted)
    assert recon["adr"].agrees is True         # ADR weighted by rooms sold = 100


def test_reconciliation_fractional_dollar_adr(db_session):
    # A cents-level fixture so a quantization bug in the weighted aggregation can't
    # hide behind whole-dollar values: computed ADR 149.99 must reconcile to the
    # ingested 149.99.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 100),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 14999),  # 14999/100 = 149.99
        _stat(db_session, "HISJ", date(2026, 1, 1), "ADR", Decimal("149.99")),
    ])
    db_session.commit()
    m = core_metrics(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), basis="as_reported")
    assert m.adr == Decimal("149.9900")
    recon = reconciliation(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1), m)
    assert recon["adr"].ingested == Decimal("149.99")
    assert recon["adr"].agrees is True


def test_prior_year_window_is_same_length_as_current_over_leap_boundary(db_session):
    # F3: current window 2024-02-29..2024-03-01 (n=2). The prior-year window must be
    # EXACTLY 2 days (2023-03-01..2023-03-02), derived from a single shifted anchor +
    # the current length — not two independently shifted endpoints (which would give
    # a 1-day 2023-03-01..2023-03-01 window and drop 2023-03-02's history).
    _prop(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2023, 1, 1), total_rooms=100),
        RoomInventory(property_id="HISJ", effective_date=date(2024, 1, 1), total_rooms=100),
    ])
    db_session.add_all([
        # current window
        _stat(db_session, "HISJ", date(2024, 2, 29), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2024, 3, 1), "ROOMS_OCCUPIED", 80),
        # prior-year window: BOTH 2023 days must be included (sold 50 + 70 over
        # avail 100 + 100 = 0.6000; a truncated 1-day window would give 0.5000).
        _stat(db_session, "HISJ", date(2023, 3, 1), "ROOMS_OCCUPIED", 50),
        _stat(db_session, "HISJ", date(2023, 3, 2), "ROOMS_OCCUPIED", 70),
    ])
    db_session.commit()
    cmp = compare(db_session, "HISJ", date(2024, 2, 29), date(2024, 3, 1), basis="as_reported")
    assert cmp.prior_year is not None
    assert cmp.prior_year.start == date(2023, 3, 1)
    assert cmp.prior_year.end == date(2023, 3, 2)   # exactly n=2 days, not truncated
    assert cmp.prior_year.occupancy == Decimal("0.6000")  # both 2023 days fed it


def test_complete_days_requires_coverage_and_availability(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    # Day 1 has a statistics report landed; day 2 does not.
    db_session.add(IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 1),
                                     report_type="manager_flash"))
    db_session.commit()
    got = complete_days(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2))
    assert got == {date(2026, 1, 1)}


def test_complete_days_excludes_day_without_inventory(db_session):
    _prop(db_session)
    # inventory starts 2026-01-02, so 2026-01-01 has coverage but no in-force count.
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 2), total_rooms=100))
    db_session.add_all([
        IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 1), report_type="manager_flash"),
        IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 2), report_type="manager_flash"),
    ])
    db_session.commit()
    assert complete_days(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2)) == {date(2026, 1, 2)}


def test_complete_days_accepts_autoclerk_manager_report(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add(IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 1),
                                     report_type="manager_report"))
    db_session.commit()
    # any-of equivalence: AUTOCLERK's manager_report satisfies the requirement
    # just as OPERA's manager_flash does.
    assert {"manager_flash", "manager_report"} <= REQUIRED_STATISTICS_REPORTS
    assert complete_days(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1)) == {date(2026, 1, 1)}
