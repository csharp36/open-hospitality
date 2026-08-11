from datetime import date
from decimal import Decimal

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from tests.test_performance_service import _seg, _stat
from tests.test_property_config_api import (
    _admin_headers,
    _client,
    _org_and_property,
)
from usali.models import (
    FiscalCalendar,
    IngestionCoverage,
    PropertyStatConfig,
    RoomInventory,
    Timecard,
    UsaliLaborFact,
)
from usali.performance import core_metrics


def test_get_performance_range(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 12000),
        _stat(db_session, "HISJ", date(2026, 1, 1), "TOTAL_REVENUE", 18000),
    ])
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&from=2026-01-01&to=2026-01-01",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["occupancy"] == "0.8000"
    assert body["adr_room_basis"] == "as_reported"


def test_performance_includes_labor_productivity(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add(_stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80))
    # UsaliLaborFact.timecard_id is NOT NULL with a composite FK onto timecard, so
    # seed a real employee + approved card for the fact to hang off (mirrors
    # tests/test_performance_service.py::test_labor_hours_per_occupied_room).
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
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&from=2026-01-01&to=2026-01-01",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    labor = r.json()["labor"]
    assert labor["labor_hours"] == "160.00"
    assert labor["rooms_sold"] == "80.0000"
    assert labor["hours_per_occupied_room"] == "2.0000"  # 160/80
    assert "cost_suppressed" in labor


def test_performance_refuses_out_of_scope(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    _org_and_property(db_session, "SSSJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "property_gm", sub="gm-sss", property_id="SSSJ")
    tok = mint(roles=["property_gm"], sub="gm-sss", scopes=[{"property_id": "SSSJ", "department_id": None}])
    assert c.get("/api/performance?property=HISJ&from=2026-01-01&to=2026-01-02",
                 headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_performance_refuses_unconfigured_inventory(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(_stat(db_session, "HISJ", date(2026, 1, 1), "ROOMS_OCCUPIED", 80))
    db_session.commit()  # no RoomInventory -> rooms_available refuses
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    assert c.get("/api/performance?property=HISJ&from=2026-01-01&to=2026-01-01",
                 headers=_admin_headers(mint, db_session)).status_code == 409


def test_performance_exclude_comp_house_segmentless_trailing_day_is_200(
    db_engine, db_session, tmp_path
):
    # exclude_comp_house property. The REQUESTED window (Jan 14) is fully
    # segment-covered, but an earlier COMPLETE day in the trailing trend window
    # (Jan 8: coverage + inventory + occupied rooms, NO segments) cannot net
    # comp/house-use from ADR. The trend must degrade gracefully — the whole
    # /api/performance response stays 200, not 409.
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add(PropertyStatConfig(property_id="HISJ", adr_room_basis="exclude_comp_house"))
    # Jan 14 (requested): segment-covered.
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 14), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 14), "ROOM_REVENUE", 8000),
        _stat(db_session, "HISJ", date(2026, 1, 14), "TOTAL_REVENUE", 12000),
        IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 14), report_type="manager_flash"),
        _seg(db_session, "HISJ", date(2026, 1, 14), "COMPLIMENTARY", 3),
        _seg(db_session, "HISJ", date(2026, 1, 14), "HOUSE_USE", 2),
        _seg(db_session, "HISJ", date(2026, 1, 14), "TRANSIENT", 75),
    ])
    # Jan 8: complete + occupied, but NO segments -> segmentless trailing day.
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 70),
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOM_REVENUE", 7000),
        _stat(db_session, "HISJ", date(2026, 1, 8), "TOTAL_REVENUE", 10000),
        IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 8), report_type="manager_flash"),
    ])
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&from=2026-01-14&to=2026-01-14",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    assert r.json()["adr_room_basis"] == "exclude_comp_house"


def test_performance_exclude_comp_house_segmentless_requested_day_still_409(
    db_engine, db_session, tmp_path
):
    # The REQUESTED-window fail-loud MUST still hold: a request whose OWN from..to
    # window has a segmentless occupied day under exclude_comp_house refuses (409).
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add(PropertyStatConfig(property_id="HISJ", adr_room_basis="exclude_comp_house"))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 70),
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOM_REVENUE", 7000),
        IngestionCoverage(property_id="HISJ", business_date=date(2026, 1, 8), report_type="manager_flash"),
    ])  # no segments for the requested day
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&from=2026-01-08&to=2026-01-08",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 409


def test_performance_exclude_comp_house_inconsistent_segments_is_409(
    db_engine, db_session, tmp_path
):
    # F1: an exclude_comp_house property whose REQUESTED window has comp+house rooms
    # meeting/exceeding occupied rooms would net a zero/negative ADR basis. That is
    # SegmentInconsistent (an AdrBasisUnavailable subclass), so the endpoint 409s
    # rather than publishing a negative ADR.
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add(PropertyStatConfig(property_id="HISJ", adr_room_basis="exclude_comp_house"))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOMS_OCCUPIED", 100),
        _stat(db_session, "HISJ", date(2026, 1, 8), "ROOM_REVENUE", 12000),
        _seg(db_session, "HISJ", date(2026, 1, 8), "COMPLIMENTARY", 60),
        _seg(db_session, "HISJ", date(2026, 1, 8), "HOUSE_USE", 50),  # 110 >= 100 occupied
    ])
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&from=2026-01-08&to=2026-01-08",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 409


def test_get_performance_by_fiscal_period(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.add(FiscalCalendar(property_id="HISJ", calendar_type="calendar_month",
                                  fiscal_year_start_month=1, week_start_weekday=None))
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 15), "ROOMS_OCCUPIED", 80),
        _stat(db_session, "HISJ", date(2026, 1, 15), "ROOM_REVENUE", 12000),
    ])
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/performance?property=HISJ&period=2026-P01", headers=_admin_headers(mint, db_session))
    assert r.status_code == 200 and r.json()["period"] == "2026-P01"


def test_performance_period_without_calendar_is_409(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.commit()  # no FiscalCalendar
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    assert c.get("/api/performance?property=HISJ&period=2026-P01",
                 headers=_admin_headers(mint, db_session)).status_code == 409


def test_performance_requires_exactly_one_of_range_or_period(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    assert c.get("/api/performance?property=HISJ", headers=h).status_code == 422  # neither
    assert c.get("/api/performance?property=HISJ&from=2026-01-01&to=2026-01-31&period=2026-P01",
                 headers=h).status_code == 422  # both


def test_performance_room_revenue_drills_through(db_engine, db_session, tmp_path, seed_six_pdfs):
    # seed_six_pdfs ingests the real sample set (2026-07-07), producing financial
    # facts + their stage rows for the Rooms line. HISJ is the OPERA property; only
    # OPERA carries the ROOM_REVENUE statistic core_metrics reads.
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1),
                                 total_rooms=100))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    PID = "HISJ"
    r = c.get(f"/api/performance/room-revenue/transactions?property={PID}"
              "&from=2026-07-07&to=2026-07-07", headers=h)
    assert r.status_code == 200
    txns = r.json()["transactions"]
    assert txns  # non-empty
    total = sum(Decimal(t["amount"]) for t in txns)
    m = core_metrics(db_session, PID, date(2026, 7, 7), date(2026, 7, 7), basis="as_reported")
    assert abs(total - m.room_revenue) <= Decimal("0.01")
