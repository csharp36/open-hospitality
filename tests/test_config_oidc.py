from usali.config import Settings


def test_oidc_defaults_point_at_local_keycloak():
    s = Settings()
    assert s.oidc_issuer == "http://localhost:9080/realms/usali"
    assert s.oidc_jwks_url == (
        "http://localhost:9080/realms/usali/protocol/openid-connect/certs"
    )
    # NOT "account": a realm client without an audience mapper issues tokens
    # with no `aud` at all, so the old default 401'd every real browser login.
    # The realm's operator-portal now emits this dedicated audience — see
    # tests/test_oidc_realm_contract.py, which pins the two together.
    assert s.oidc_audience == "usali-api"


def test_oidc_env_override(monkeypatch):
    monkeypatch.setenv("USALI_OIDC_ISSUER", "https://id.example.com/realms/prod")
    assert Settings().oidc_issuer == "https://id.example.com/realms/prod"
