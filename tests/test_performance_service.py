from datetime import date
from decimal import Decimal

import pytest

from tests.employees import make_employee
from usali.models import (
    IngestBatch,
    IngestionCoverage,
    Organization,
    PmsDailyStatisticStage,
    Property,
    RoomInventory,
    Timecard,
    UsaliLaborFact,
    UsaliSegmentFact,
    UsaliStatisticFact,
)
from usali.performance import (
    REQUIRED_STATISTICS_REPORTS,
    AdrBasisUnavailable,
    CoreMetrics,
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
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
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
