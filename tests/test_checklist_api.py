from fastapi.testclient import TestClient

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import IngestBatch, Organization
from usali.server import create_app


def _client(db_engine, tmp_path, verifier) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app)


def _org(db_session):
    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.commit()


def _admin_headers(mint, db_session, sub="cl-admin"):
    grant_role(db_session, "org_admin", sub=sub, org_id=1)
    return {"Authorization": f"Bearer {mint(roles=['org_admin'], sub=sub)}"}


def test_get_checklist_reports_open_items(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/checklist", headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    body = r.json()
    assert body["all_clear"] is False
    assert body["open_count"] == len(body["items"])
    first = {i["key"]: i for i in body["items"]}["first_report"]
    assert first["status"] == "open"
    assert first["required"] is True
    assert first["where"] == "/upload"


def test_get_checklist_marks_done_items(db_engine, db_session, tmp_path):
    _org(db_session)
    db_session.add(IngestBatch(org_id=1, pms_source="OPERA", report_type="trial_balance",
                               source_file="f.pdf", file_hash="h"))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/checklist", headers=_admin_headers(mint, db_session))
    items = {i["key"]: i for i in r.json()["items"]}
    assert items["first_report"]["status"] == "done"
