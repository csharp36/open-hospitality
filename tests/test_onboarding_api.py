from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import Department, Organization, Property
from usali.server import create_app
from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _client(db_engine: Engine, tmp_path: Path, verifier, kc):
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        keycloak_admin=kc,
    )
    return TestClient(app)


def test_create_app_accepts_keycloak_admin(db_engine, db_session, tmp_path):
    verifier, mint = make_authkit()
    kc = InMemoryKeycloakAdmin()
    c = _client(db_engine, tmp_path, verifier, kc)
    r = c.get("/api/properties")
    assert r.status_code == 401  # no token → still gated, app built fine


def _seed_property(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.commit()
    # L4: role authority is DB grants — scope still comes from the claim
    # (the 'gm' sub's HISJ claim is what keeps SSSJ refused).
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")


def _body(**over):
    b = {"full_name": "Gina M", "email": "gina@x.com", "property": "HISJ",
         "pay_type": "salary", "role": "property_gm"}
    b.update(over)
    return b


def test_org_admin_can_onboard(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    r = c.post("/api/employees", json=_body(),
               headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 201
    assert r.json()["keycloak_subject"] is not None


def test_accountant_cannot_onboard(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    r = c.post("/api/employees", json=_body(),
               headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 403


def test_gm_can_onboard_into_own_property_only(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    dept = Department(property_id="HISJ", name="Front Office")
    db_session.add(dept)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "HISJ", "department_id": None}])
    ok = c.post("/api/employees",
                json=_body(property="HISJ", role="department_manager", department_id=dept.department_id),
                headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 201
    denied = c.post("/api/employees",
                    json=_body(property="SSSJ", role="department_manager",
                               department_id=dept.department_id),
                    headers={"Authorization": f"Bearer {tok}"})
    assert denied.status_code == 403


def test_operator_role_without_email_is_422(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    r = c.post("/api/employees", json=_body(email=None),
               headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 422


def test_hourly_without_role_or_email_ok(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    r = c.post("/api/employees", json=_body(email=None, role=None),
               headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 201
    assert r.json()["keycloak_subject"] is None


def test_terminate_marks_and_disables(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    verifier, mint = make_authkit()
    kc = InMemoryKeycloakAdmin()
    c = _client(db_engine, tmp_path, verifier, kc)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    created = c.post("/api/employees", json=_body(), headers=admin).json()
    r = c.post(f"/api/employees/{created['employee_id']}/terminate", headers=admin)
    assert r.status_code == 200
    assert r.json()["termination_date"] is not None
    assert kc.users[created["keycloak_subject"]]["enabled"] is False


def test_me_returns_roles(db_engine, db_session, tmp_path):
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    r = c.get("/api/me",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    assert r.json()["subject"] == "adm"
    assert "org_admin" in r.json()["roles"]


def test_department_manager_without_department_is_422(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    r = c.post("/api/employees", json=_body(role="department_manager"),
               headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 422


def test_gm_with_global_role_still_confined_to_own_property(db_engine, db_session, tmp_path):
    # A property_gm who ALSO holds a global view role must NOT gain onboard-anywhere.
    _seed_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    tok = mint(roles=["property_gm", "accountant"], sub="gm",
               scopes=[{"property_id": "HISJ", "department_id": None}])
    denied = c.post("/api/employees", json=_body(property="SSSJ", role="property_gm"),
                    headers={"Authorization": f"Bearer {tok}"})
    assert denied.status_code == 403


def test_gm_cannot_terminate_other_property(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryKeycloakAdmin())
    # org_admin onboards an SSSJ employee.
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    created = c.post("/api/employees", json=_body(property="SSSJ"), headers=admin).json()
    # A HISJ-scoped GM cannot terminate the SSSJ employee.
    gm = mint(roles=["property_gm"], sub="gm", scopes=[{"property_id": "HISJ", "department_id": None}])
    r = c.post(f"/api/employees/{created['employee_id']}/terminate",
               headers={"Authorization": f"Bearer {gm}"})
    assert r.status_code == 403
