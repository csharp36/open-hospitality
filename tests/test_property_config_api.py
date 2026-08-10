from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import func, select as _select

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    FiscalCalendar,
    Organization,
    OutOfOrderRoom,
    Property,
    RoomInventory,
)
from usali.server import create_app


def _client(db_engine, tmp_path, verifier) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app)


def _org_and_property(db_session, pid="HISJ"):
    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    db_session.commit()


def _admin_headers(mint, db_session):
    grant_role(db_session, "org_admin", sub="cfg-admin", org_id=1)
    tok = mint(roles=["org_admin"], sub="cfg-admin")
    return {"Authorization": f"Bearer {tok}"}


def test_get_config_returns_inventory_ooo_and_fiscal(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140),
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 2, 1), end_date=date(2026, 2, 7),
                       room_count=3, reason_code="renovation"),
        FiscalCalendar(property_id="HISJ", calendar_type="calendar_month",
                       fiscal_year_start_month=1, week_start_weekday=None),
    ])
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/config", headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    body = r.json()
    assert body["inventory"][0]["total_rooms"] == 140
    assert body["out_of_order"][0]["reason_code"] == "renovation"
    assert body["fiscal_calendar"]["calendar_type"] == "calendar_month"


def test_get_rooms_available(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/rooms-available?start=2026-01-01&end=2026-01-31",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200 and r.json()["room_nights"] == 4340


def test_rooms_available_refuses_unconfigured_window(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/rooms-available?start=2025-12-01&end=2026-01-05",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 409  # fail-loud, named


def test_get_fiscal_periods_enumerates(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    db_session.add(FiscalCalendar(property_id="HISJ", calendar_type="calendar_month",
                                  fiscal_year_start_month=1, week_start_weekday=None))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties/HISJ/fiscal-periods?fiscal_year=2026",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    periods = r.json()["periods"]
    assert len(periods) == 12 and periods[0]["key"] == "2026-P01"


def test_read_refuses_out_of_scope_property(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    # The other hotel has to EXIST for a grant to name it — the composite FK
    # (org_id, property_id) is the tenancy wall doing its job (same idiom as
    # test_workforce_api.test_gm_cannot_touch_a_rate_at_another_property).
    _org_and_property(db_session, "SSSJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    # A GM scoped to a DIFFERENT property.
    grant_role(db_session, "property_gm", sub="gm-other", property_id="SSSJ")
    tok = mint(roles=["property_gm"], sub="gm-other",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    r = c.get("/api/properties/HISJ/config", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_post_inventory_creates_and_audits(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 140}, headers=h)
    assert r.status_code == 201
    db_session.expire_all()
    assert db_session.execute(_select(func.count()).select_from(RoomInventory)).scalar_one() == 1
    audits = db_session.execute(
        _select(AuditEvent).where(AuditEvent.action == "property_inventory_set")
    ).scalars().all()
    assert len(audits) == 1 and audits[0].resource_id == "HISJ"


def test_post_inventory_same_date_is_a_correction(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    c.post("/api/properties/HISJ/inventory",
           json={"effective_date": "2026-01-01", "total_rooms": 140}, headers=h)
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 145}, headers=h)
    assert r.status_code == 201
    db_session.expire_all()
    rows = db_session.execute(_select(RoomInventory)).scalars().all()
    assert len(rows) == 1 and rows[0].total_rooms == 145  # upsert, not a duplicate


def test_post_inventory_rejects_nonpositive(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 0},
               headers=_admin_headers(mint, db_session))
    assert r.status_code == 422


def test_post_and_delete_ooo(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    r = c.post("/api/properties/HISJ/out-of-order",
               json={"start_date": "2026-02-01", "end_date": "2026-02-07",
                     "room_count": 3, "reason_code": "renovation"}, headers=h)
    assert r.status_code == 201
    ooo_id = r.json()["ooo_id"]
    db_session.expire_all()
    added = db_session.execute(
        _select(AuditEvent).where(AuditEvent.action == "ooo_added")
    ).scalars().all()
    assert len(added) == 1 and added[0].resource_id == "HISJ"
    r2 = c.delete(f"/api/properties/HISJ/out-of-order/{ooo_id}", headers=h)
    assert r2.status_code == 204
    db_session.expire_all()
    assert db_session.execute(_select(func.count()).select_from(OutOfOrderRoom)).scalar_one() == 0
    removed = db_session.execute(
        _select(AuditEvent).where(AuditEvent.action == "ooo_removed")
    ).scalars().all()
    assert len(removed) == 1 and removed[0].resource_id == "HISJ"


def test_post_ooo_rejects_bad_reason_and_backwards_range(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    assert c.post("/api/properties/HISJ/out-of-order",
                  json={"start_date": "2026-02-01", "end_date": "2026-02-07",
                        "room_count": 3, "reason_code": "bogus"}, headers=h).status_code == 422
    assert c.post("/api/properties/HISJ/out-of-order",
                  json={"start_date": "2026-02-07", "end_date": "2026-02-01",
                        "room_count": 3, "reason_code": "damage"}, headers=h).status_code == 422


def test_put_fiscal_calendar_requires_weekday_for_445(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    # 445 without a weekday -> 422 at the boundary
    assert c.put("/api/properties/HISJ/fiscal-calendar",
                 json={"calendar_type": "445", "fiscal_year_start_month": 1}, headers=h).status_code == 422
    # calendar_month with a weekday -> 422
    assert c.put("/api/properties/HISJ/fiscal-calendar",
                 json={"calendar_type": "calendar_month", "fiscal_year_start_month": 1,
                       "week_start_weekday": 6}, headers=h).status_code == 422
    # valid 445 upserts
    r = c.put("/api/properties/HISJ/fiscal-calendar",
              json={"calendar_type": "445", "fiscal_year_start_month": 1, "week_start_weekday": 6},
              headers=h)
    assert r.status_code == 200
    db_session.expire_all()
    row = db_session.get(FiscalCalendar, "HISJ")
    assert row.calendar_type == "445" and row.week_start_weekday == 6
    audits = db_session.execute(
        _select(AuditEvent).where(AuditEvent.action == "fiscal_calendar_set")
    ).scalars().all()
    assert len(audits) == 1 and audits[0].resource_id == "HISJ"


def test_write_refuses_out_of_scope_and_audits_nothing_extra(db_engine, db_session, tmp_path):
    _org_and_property(db_session)
    # The other hotel has to EXIST for a grant to name it — the composite FK
    # (org_id, property_id) is the tenancy wall doing its job (same fix Task 5
    # applied to test_read_refuses_out_of_scope_property).
    _org_and_property(db_session, "SSSJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "property_gm", sub="gm-sss", property_id="SSSJ")
    tok = mint(roles=["property_gm"], sub="gm-sss",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    r = c.post("/api/properties/HISJ/inventory",
               json={"effective_date": "2026-01-01", "total_rooms": 140},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    db_session.expire_all()
    audits = db_session.execute(
        _select(AuditEvent).where(AuditEvent.resource_id == "HISJ")
    ).scalars().all()
    assert len(audits) == 0
