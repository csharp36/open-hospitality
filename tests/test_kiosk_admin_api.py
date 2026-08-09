from fastapi.testclient import TestClient

from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import KioskDevice, Organization, Property
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    return TestClient(app)


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.commit()
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "accountant")  # the default sub
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")


def test_org_admin_enrolls_device_and_token_is_shown_once(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.post("/api/kiosk-devices", json={"property": "HISJ", "name": "Back office iPad"},
               headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 201
    body = r.json()
    assert body["token"]  # plaintext, shown exactly once
    assert body["property_id"] == "HISJ"

    device = db_session.get(KioskDevice, body["device_id"])
    assert device is not None
    assert device.token_hash != body["token"]   # stored hashed, never plaintext
    assert len(device.token_hash) == 64          # sha256 hex


def test_accountant_cannot_enroll(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.post("/api/kiosk-devices", json={"property": "HISJ", "name": "x"},
               headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 403


def test_gm_confined_to_own_property(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "HISJ", "department_id": None}])
    ok = c.post("/api/kiosk-devices", json={"property": "HISJ", "name": "ok"},
                headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 201
    denied = c.post("/api/kiosk-devices", json={"property": "SSSJ", "name": "no"},
                    headers={"Authorization": f"Bearer {tok}"})
    assert denied.status_code == 403


def test_revoke_marks_device(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    created = c.post("/api/kiosk-devices", json={"property": "HISJ", "name": "iPad"},
                     headers=admin).json()
    r = c.post(f"/api/kiosk-devices/{created['device_id']}/revoke", headers=admin)
    assert r.status_code == 200
    assert r.json()["revoked"] is True


def test_enroll_unknown_property_is_404(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.post("/api/kiosk-devices", json={"property": "NOPE", "name": "x"},
               headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 404
