from datetime import date

from tests.authkit import make_authkit
from tests.grants import grant_role
from tests.test_performance_service import _stat
from tests.test_property_config_api import (
    _admin_headers,
    _client,
    _org_and_property,
)
from usali.models import RoomInventory


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
