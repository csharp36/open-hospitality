from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine

from usali.db import make_session_factory
from usali.server import create_app
from tests.authkit import make_authkit


def _client(db_engine: Engine, tmp_path: Path, verifier) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
    )
    return TestClient(app)


def test_properties_requires_auth(db_engine, db_session, tmp_path):
    verifier, _ = make_authkit()
    r = _client(db_engine, tmp_path, verifier).get("/api/properties")
    assert r.status_code == 401


def test_properties_forbidden_for_employee(db_engine, db_session, tmp_path):
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties", headers={"Authorization": f"Bearer {mint(roles=['employee'])}"})
    assert r.status_code == 403


def test_properties_ok_for_accountant(db_engine, db_session, founding_org, tmp_path):
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/properties", headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 200


def test_malformed_scopes_claim_is_401_not_500(db_engine, db_session, tmp_path):
    # A signed token whose `scopes` claim has the wrong shape must be treated as
    # a bad token (401), never crash claim parsing into a 500.
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["accountant"], extra_claims={"scopes": "everything"})
    r = c.get("/api/properties", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_openapi_schema_not_exposed(db_engine, db_session, tmp_path):
    empty_dist = tmp_path / "no_dist"  # does not exist -> no SPA mount
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        dist_dir=empty_dist,
    )
    r = TestClient(app).get("/openapi.json")
    assert r.status_code == 404
