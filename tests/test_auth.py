import base64
import hashlib
import hmac
import json
import time

import pytest
from jwt import PyJWKClientConnectionError, PyJWKClientError

from usali.auth import AuthError, AuthUnavailableError, Principal, TokenVerifier
from tests.authkit import make_authkit


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _forge_hs256(secret: str, *, kid: str = "test-key-1") -> str:
    """Hand-craft an HS256 JWT using `secret` as the HMAC key.

    PyJWT's encoder refuses a PEM public key as an HMAC secret (its own RS/HS
    guard), so an attacker forging a confusion token would build it by hand like
    this. We do the same to prove `verify()` rejects it independently.
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    payload = {
        "sub": "attacker", "iss": "https://test-issuer/realms/usali",
        "aud": "account", "iat": now, "exp": now + 300,
        "preferred_username": "attacker", "realm_access": {"roles": ["org_admin"]},
    }
    signing_input = (
        _b64url(json.dumps(header).encode()) + b"." + _b64url(json.dumps(payload).encode())
    )
    sig = _b64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return (signing_input + b"." + sig).decode()


def test_valid_token_yields_principal_with_roles():
    verifier, mint = make_authkit()
    p = verifier.verify(mint(roles=["accountant", "org_admin"], sub="alice"))
    assert isinstance(p, Principal)
    assert p.subject == "alice"
    assert p.username == "alice"
    assert p.roles == frozenset({"accountant", "org_admin"})


def test_missing_realm_access_gives_empty_roles():
    verifier, mint = make_authkit()
    p = verifier.verify(mint(roles=[]))
    assert p.roles == frozenset()


def test_expired_token_rejected():
    verifier, mint = make_authkit()
    # Expire well beyond the 60s clock-skew leeway so this exercises real expiry.
    with pytest.raises(AuthError):
        verifier.verify(mint(roles=["accountant"], exp_in=-120))


def test_wrong_issuer_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError):
        verifier.verify(mint(roles=["accountant"], issuer="https://evil/realms/x"))


def test_wrong_audience_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError):
        verifier.verify(mint(roles=["accountant"], audience="not-account"))


def test_tampered_signature_rejected():
    verifier, mint = make_authkit()
    token = mint(roles=["accountant"])
    tampered = token[:-3] + ("abc" if not token.endswith("abc") else "xyz")
    with pytest.raises(AuthError):
        verifier.verify(tampered)


def test_alg_none_rejected():
    verifier, mint = make_authkit()
    token = mint(roles=["accountant"], algorithm="none", key=None)
    with pytest.raises(AuthError):
        verifier.verify(token)


def test_rs_hs_confusion_rejected():
    # Attacker signs HS256 using the public key (PEM) as the HMAC secret,
    # hoping the verifier treats the public key as a symmetric secret.
    verifier, mint = make_authkit()
    token = _forge_hs256(mint.public_pem)
    with pytest.raises(AuthError):
        verifier.verify(token)


def test_unknown_kid_rejected():
    verifier, mint = make_authkit()
    token = mint(roles=["accountant"], kid="nope")
    with pytest.raises(AuthError):
        verifier.verify(token)


def test_missing_sub_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError):
        verifier.verify(mint(roles=["accountant"], omit_sub=True))


def test_absent_realm_access_gives_empty_roles():
    verifier, mint = make_authkit()
    p = verifier.verify(mint(omit_realm_access=True))
    assert isinstance(p, Principal)
    assert p.roles == frozenset()


# --- malformed-but-signed claim shapes: must be AuthError (401), never a 500 ---


def test_scopes_claim_as_string_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError) as exc_info:
        verifier.verify(mint(roles=["property_gm"], extra_claims={"scopes": "everything"}))
    assert "scopes" in str(exc_info.value)
    assert "everything" not in str(exc_info.value)  # never echo claim values


def test_scopes_entry_missing_property_id_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError) as exc_info:
        verifier.verify(mint(roles=["property_gm"], scopes=[{"department_id": 1}]))
    assert "scopes" in str(exc_info.value)


def test_scopes_entry_non_dict_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError) as exc_info:
        verifier.verify(
            mint(roles=["property_gm"], extra_claims={"scopes": ["HISJ"]})
        )
    assert "scopes" in str(exc_info.value)
    assert "HISJ" not in str(exc_info.value)


def test_scopes_entry_string_department_id_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError) as exc_info:
        verifier.verify(
            mint(roles=["property_gm"],
                 scopes=[{"property_id": "HISJ", "department_id": "abc"}])
        )
    assert "scopes" in str(exc_info.value)
    assert "abc" not in str(exc_info.value)


def test_realm_access_claim_as_string_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError) as exc_info:
        verifier.verify(mint(extra_claims={"realm_access": "org_admin"}))
    assert "realm_access" in str(exc_info.value)
    assert "org_admin" not in str(exc_info.value)


def test_non_string_role_entry_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError) as exc_info:
        verifier.verify(
            mint(extra_claims={"realm_access": {"roles": ["org_admin", 42]}})
        )
    assert "realm_access" in str(exc_info.value)


def test_non_list_roles_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError):
        verifier.verify(mint(extra_claims={"realm_access": {"roles": "org_admin"}}))


def test_non_string_sub_rejected():
    verifier, mint = make_authkit()
    with pytest.raises(AuthError):
        verifier.verify(mint(extra_claims={"sub": 12345}))


def test_well_formed_scopes_token_still_verifies():
    verifier, mint = make_authkit()
    p = verifier.verify(mint(
        roles=["property_gm"],
        scopes=[{"property_id": "HISJ", "department_id": None},
                {"property_id": "SSSJ", "department_id": 3}],
    ))
    assert p.roles == frozenset({"property_gm"})
    assert p.scopes == frozenset({("HISJ", None), ("SSSJ", 3)})


def test_prod_path_jwks_error_maps_to_auth_error():
    def resolve(kid: str) -> object:
        raise PyJWKClientError("no signing key for kid")

    verifier = TokenVerifier(
        issuer="https://test-issuer/realms/usali",
        audience="account",
        signing_key_resolver=resolve,
    )
    _, mint = make_authkit()
    with pytest.raises(AuthError):
        verifier.verify(mint(roles=["accountant"]))


def test_prod_path_connection_error_maps_to_unavailable():
    def resolve(kid: str) -> object:
        raise PyJWKClientConnectionError("jwks endpoint unreachable")

    verifier = TokenVerifier(
        issuer="https://test-issuer/realms/usali",
        audience="account",
        signing_key_resolver=resolve,
    )
    _, mint = make_authkit()
    with pytest.raises(AuthUnavailableError):
        verifier.verify(mint(roles=["accountant"]))
