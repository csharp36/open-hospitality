from dataclasses import replace

from fastapi.testclient import TestClient

import usali.checklist as checklist_module
from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import IngestBatch, OrgChecklistOverride, Organization
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


def test_get_checklist_surfaces_a_probe_error_and_withholds_all_clear(
    db_engine, db_session, tmp_path, monkeypatch
):
    """A probe that raises must reach the client as `error`, and must NOT be
    countable as clear: `all_clear` requires zero errors as well as zero
    open items (an unchecked item is not a finished item).

    Everything BUT `team` is driven to non-open on purpose: the three
    required items are made to report `done`, and the remaining optional
    items are dismissed via a real override row. That pushes `open_count`
    to 0 while one item still errors — the only way to tell the fixed
    formula (`open_count == 0 and error_count == 0`) apart from the old,
    buggy one (`open_count == 0`), which would call this `all_clear` too.
    """
    _org(db_session)

    def _boom(_session):
        raise RuntimeError("probe exploded")

    def _done(_session):
        return True

    required_keys = {"first_report", "room_inventory", "fiscal_calendar"}
    dismissed_keys = {"payroll", "accounting", "demand_feed"}

    def _patched(item):
        if item.key == "team":
            return replace(item, probe=_boom)
        if item.key in required_keys:
            return replace(item, probe=_done)
        return item  # payroll/accounting/demand_feed: probes already say "not done"

    monkeypatch.setattr(
        checklist_module, "ITEMS", tuple(_patched(item) for item in checklist_module.ITEMS)
    )
    for key in dismissed_keys:
        db_session.add(OrgChecklistOverride(org_id=1, item_key=key, created_by="setup"))
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/checklist", headers=_admin_headers(mint, db_session))
    assert r.status_code == 200
    body = r.json()
    items = {i["key"]: i for i in body["items"]}
    assert items["team"]["status"] == "error"
    assert {items[k]["status"] for k in dismissed_keys} == {"dismissed"}
    assert body["open_count"] == 0
    assert body["error_count"] == 1
    assert body["all_clear"] is False


def test_get_checklist_requires_authentication(db_engine, tmp_path):
    verifier, _mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/checklist")
    assert r.status_code == 401


def test_dismissing_an_optional_item_hides_it_from_the_open_count(
    db_engine, db_session, tmp_path
):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    before = c.get("/api/checklist", headers=h).json()["open_count"]
    r = c.put("/api/checklist/payroll/dismissal", json={"note": "we use a bureau"},
              headers=h)
    assert r.status_code == 204
    body = c.get("/api/checklist", headers=h).json()
    assert body["open_count"] == before - 1
    assert {i["key"]: i for i in body["items"]}["payroll"]["status"] == "dismissed"


def test_dismissal_is_idempotent(db_engine, db_session, tmp_path):
    """D-B4.5: two browser sessions dismissing at once must not 500."""
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    assert c.put("/api/checklist/payroll/dismissal", headers=h).status_code == 204
    assert c.put("/api/checklist/payroll/dismissal", headers=h).status_code == 204


def test_undismissing_is_idempotent_too(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    h = _admin_headers(mint, db_session)
    c.put("/api/checklist/payroll/dismissal", headers=h)
    assert c.delete("/api/checklist/payroll/dismissal", headers=h).status_code == 204
    assert c.delete("/api/checklist/payroll/dismissal", headers=h).status_code == 204
    body = c.get("/api/checklist", headers=h).json()
    assert {i["key"]: i for i in body["items"]}["payroll"]["status"] == "open"


def test_dismissing_a_required_item_refuses_loudly(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.put("/api/checklist/room_inventory/dismissal",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 422
    assert "required" in r.json()["detail"]


def test_unknown_item_key_is_404(db_engine, db_session, tmp_path):
    _org(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.put("/api/checklist/no_such_item/dismissal",
              headers=_admin_headers(mint, db_session))
    assert r.status_code == 404


def test_a_non_admin_operator_cannot_dismiss(db_engine, db_session, tmp_path):
    _org(db_session)
    grant_role(db_session, "accountant", sub="bookkeeper", org_id=1)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["accountant"], sub="bookkeeper")
    r = c.put("/api/checklist/payroll/dismissal",
              headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
