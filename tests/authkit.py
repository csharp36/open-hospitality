"""Offline auth test kit: an RSA keypair, a TokenVerifier wired to it, and a
token minter. Lets endpoint tests authenticate without running Keycloak."""

import time
from collections.abc import Callable
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from usali.auth import Principal, TokenVerifier

_ISSUER = "https://test-issuer/realms/usali"
_AUDIENCE = "usali-api"
_KID = "test-key-1"
# The founding org's Keycloak alias — what the dev realm's organization
# emits (KC 26 organization-membership mapper, default config: a JSON
# array of alias strings). Minted by default so the harness matches what
# the realm actually issues for every persona; tests/test_oidc_realm_contract
# pins the agreement. `organizations=None` omits the claim (a user in no
# org — KC omits the claim entirely rather than emitting []).
DEFAULT_ORG_ALIAS = "pilot-hotel-group"

# Sentinel distinguishing "caller did not override the key" (use the RSA private
# key) from an explicit `key=None` (needed for the alg="none" attack test).
_USE_DEFAULT_KEY = object()


def make_authkit() -> tuple[TokenVerifier, Callable[..., str]]:
    """Return (verifier, mint_token). `mint_token(roles=[...], sub=..., exp_in=...)`
    signs an RS256 JWT the verifier accepts."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    def resolve(kid: str) -> object:
        if kid != _KID:
            raise KeyError(kid)
        return public_key

    verifier = TokenVerifier(
        issuer=_ISSUER, audience=_AUDIENCE, signing_key_resolver=resolve
    )

    def mint_token(
        *, roles: list[str] | None = None, sub: str = "user-1", exp_in: int = 300,
        issuer: str = _ISSUER, audience: str = _AUDIENCE,
        kid: str = _KID, omit_sub: bool = False, omit_realm_access: bool = False,
        algorithm: str = "RS256", key: object = _USE_DEFAULT_KEY,
        scopes: list[dict[str, object]] | None = None,
        organizations: tuple[str, ...] | list[str] | None = (DEFAULT_ORG_ALIAS,),
        extra_claims: dict[str, object] | None = None,
    ) -> str:
        now = int(time.time())
        payload = {
            "iss": issuer, "aud": audience,
            "iat": now, "exp": now + exp_in,
            "preferred_username": sub,
        }
        if not omit_sub:
            payload["sub"] = sub
        if not omit_realm_access:
            payload["realm_access"] = {"roles": roles or []}
        if scopes is not None:
            payload["scopes"] = scopes
        if organizations is not None:
            payload["organization"] = list(organizations)
        if extra_claims:
            # Applied last: lets malformed-claim tests override any shape above.
            payload.update(extra_claims)
        signing_key: Any = _default_key if key is _USE_DEFAULT_KEY else key
        return jwt.encode(
            payload, signing_key, algorithm=algorithm, headers={"kid": kid}
        )

    _default_key = key

    # Expose the PEM public key non-breakingly (RS/HS confusion test uses it as
    # the HMAC secret) without changing the (verifier, mint_token) return arity.
    mint_token.public_pem = public_pem  # type: ignore[attr-defined]

    return verifier, mint_token


__all__ = ["make_authkit", "Principal"]
