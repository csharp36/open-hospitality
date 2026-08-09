from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jwt import PyJWKClientConnectionError

from tests.authkit import make_authkit
from usali.auth import Principal, TokenVerifier, require_auth, require_operator


def _app(verifier):
    app = FastAPI()
    app.state.token_verifier = verifier

    @app.get("/me")
    def me(p: Principal = Depends(require_auth)):
        return {"user": p.username, "roles": sorted(p.roles)}

    @app.get("/ops")
    def ops(p: Principal = Depends(require_operator)):
        return {"ok": True}

    return app


def test_no_bearer_is_401():
    verifier, _ = make_authkit()
    r = TestClient(_app(verifier)).get("/me")
    assert r.status_code == 401
    # RFC 6750: a 401 from a Bearer scheme must carry the challenge header.
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_bad_bearer_is_401():
    verifier, _ = make_authkit()
    c = TestClient(_app(verifier))
    r = c.get("/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


def test_valid_bearer_returns_principal():
    verifier, mint = make_authkit()
    c = TestClient(_app(verifier))
    r = c.get("/me", headers={"Authorization": f"Bearer {mint(roles=['employee'])}"})
    assert r.status_code == 200
    assert r.json()["roles"] == ["employee"]


def test_operator_gate_forbids_employee_only():
    verifier, mint = make_authkit()
    c = TestClient(_app(verifier))
    r = c.get("/ops", headers={"Authorization": f"Bearer {mint(roles=['employee'])}"})
    assert r.status_code == 403


def test_operator_gate_allows_accountant():
    verifier, mint = make_authkit()
    c = TestClient(_app(verifier))
    r = c.get("/ops", headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 200


def test_payroll_admin_is_an_operator_role():
    verifier, mint = make_authkit()
    c = TestClient(_app(verifier))
    r = c.get("/ops", headers={"Authorization": f"Bearer {mint(roles=['payroll_admin'])}"})
    assert r.status_code == 200


def test_jwks_unreachable_is_503():
    def down_resolver(kid: str):
        raise PyJWKClientConnectionError("jwks endpoint unreachable")

    verifier = TokenVerifier(
        issuer="https://test-issuer/realms/usali",
        audience="account",
        signing_key_resolver=down_resolver,
    )
    # Any well-formed-looking bearer triggers the resolver, which raises the
    # connection error before signature checks -> AuthUnavailableError -> 503.
    _, mint = make_authkit()
    c = TestClient(_app(verifier))
    r = c.get("/me", headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 503
