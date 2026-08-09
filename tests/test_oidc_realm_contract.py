"""The Keycloak realm file must agree with the resource server's expectations.

This closes a real gap: every auth test mints its own token via tests/authkit.py,
so the app was validated against a token the TEST HARNESS produced, never one the
REAL realm produces. The harness was more generous than Keycloak — it always set
`aud`, while the realm's operator-portal client (no audience mapper) issued tokens
with NO `aud` claim at all. Result: 700+ green tests and a 401-on-every-request
login loop the first time a human signed in through a browser.

These are cheap static contract checks over the committed realm JSON. They do not
boot Keycloak; they assert that what the realm is configured to emit is what the
verifier is configured to require.
"""

import json
from pathlib import Path

from usali.config import Settings

_REALM_PATH = Path(__file__).resolve().parents[1] / "keycloak" / "realm-usali.json"
_OPERATOR_CLIENT = "operator-portal"


def _realm() -> dict:
    return json.loads(_REALM_PATH.read_text())


def _client(client_id: str) -> dict:
    match = [c for c in _realm()["clients"] if c["clientId"] == client_id]
    assert match, f"realm has no client {client_id!r}"
    return match[0]


def test_operator_client_emits_the_audience_the_api_requires() -> None:
    """The whole bug in one assertion: the mapper's audience must equal the
    verifier's expected `aud`, or every real login 401s."""
    mappers = _client(_OPERATOR_CLIENT).get("protocolMappers", [])
    audiences = [
        m["config"]["included.custom.audience"]
        for m in mappers
        if m.get("protocolMapper") == "oidc-audience-mapper"
    ]
    assert audiences, (
        f"{_OPERATOR_CLIENT} has no oidc-audience-mapper: Keycloak would issue "
        "tokens with no `aud` claim and the API would reject every request"
    )
    assert Settings().oidc_audience in audiences, (
        f"realm emits aud={audiences}, but the API requires "
        f"{Settings().oidc_audience!r}"
    )


def test_audience_mapper_targets_the_access_token() -> None:
    """The API reads the ACCESS token; an id-token-only mapper would not help."""
    mapper = next(
        m
        for m in _client(_OPERATOR_CLIENT)["protocolMappers"]
        if m.get("protocolMapper") == "oidc-audience-mapper"
    )
    assert mapper["config"]["access.token.claim"] == "true"


def test_test_harness_audience_matches_the_realm() -> None:
    """authkit is the double for Keycloak; if it mints a different `aud` than the
    realm emits, the suite validates a token production never issues."""
    from tests.authkit import _AUDIENCE

    assert _AUDIENCE == Settings().oidc_audience


def test_dev_user_profile_is_complete() -> None:
    """A user missing firstName/lastName trips Keycloak's UPDATE_PROFILE required
    action on first sign-in — harmless but confusing, and it was the symptom that
    surfaced the audience bug."""
    users = {u["username"]: u for u in _realm().get("users", [])}
    dev = users.get("dev-accountant")
    assert dev is not None, "realm no longer seeds dev-accountant"
    for field in ("email", "firstName", "lastName"):
        assert dev.get(field), f"dev-accountant is missing {field}"


def test_operator_client_redirects_cover_the_vite_dev_server() -> None:
    """The SPA's redirect_uri is `${origin}/callback`; the dev origin must be
    registered or Keycloak refuses the round trip."""
    client = _client(_OPERATOR_CLIENT)
    assert any("localhost:5173" in uri for uri in client["redirectUris"])
    assert "http://localhost:5173" in client["webOrigins"]


# --- L3: the organization-membership claim contract ---------------------------
#
# The org-resolution path (decision 3) trusts the token's `organization`
# claim for MEMBERSHIP only (aliases; the DB resolves alias -> org_id).
# KC 26's organization-membership mapper at its DEFAULT config emits the
# claim as a JSON array of alias strings — verified empirically against
# quay.io/keycloak/keycloak:26.0 (26.0.8): a dev-gm password grant with
# no scope parameter yielded `"organization": ["pilot-hotel-group"]` in
# the ACCESS token. Two realm-side conditions make that happen, both
# pinned here: the realm's organization exists with the personas as
# members, and the built-in `organization` client scope is a DEFAULT
# scope of the operator client — Keycloak adds the claim only when the
# literal `organization` scope is GRANTED (a string match on the
# effective scope, not a mapper-attachment question), and the SPA does
# not send a scope override.

_DEV_ORG_ALIAS = "pilot-hotel-group"
_PERSONAS = frozenset({"dev-gm", "dev-payroll", "dev-admin", "dev-accountant"})


def _dev_org() -> dict:
    orgs = _realm().get("organizations", [])
    match = [o for o in orgs if o.get("alias") == _DEV_ORG_ALIAS]
    assert match, f"realm has no organization with alias {_DEV_ORG_ALIAS!r}"
    return match[0]


def test_realm_enables_organizations_and_seeds_the_dev_org() -> None:
    realm = _realm()
    assert realm.get("organizationsEnabled") is True, (
        "organizationsEnabled must be true or Keycloak never creates the "
        "`organization` client scope and no token carries the claim"
    )
    org = _dev_org()
    assert org.get("enabled", True) is True
    # KC 26 refuses an organization without at least one domain.
    assert org.get("domains"), "the dev organization needs a domain"


def test_every_operator_persona_is_a_member_of_the_dev_org() -> None:
    """A persona missing here logs in fine and then 403s on every API
    call (no membership claim -> no active org) — the exact class of
    all-green-tests, broken-first-login bug this file exists for."""
    members = {m.get("username") for m in _dev_org().get("members", [])}
    assert _PERSONAS <= members, (
        f"personas missing from the dev organization: {_PERSONAS - members}"
    )


def test_the_organization_scope_is_a_default_scope_of_the_operator_client() -> None:
    """The claim is gated on the GRANTED scope string: as an optional
    scope (Keycloak's own default when organizations are enabled) the
    claim only appears if the SPA requests `scope=organization`, which
    it does not — the scope must be DEFAULT for the claim to land in
    every access token."""
    scopes = _client(_OPERATOR_CLIENT).get("defaultClientScopes", [])
    assert "organization" in scopes
    # Taking ownership of defaultClientScopes REPLACES Keycloak's
    # built-in assignment — the standard scopes the verifier depends on
    # must be restated, or realm_access/preferred_username/sub vanish
    # from tokens and every request 401s.
    for required in ("roles", "profile", "basic", "email"):
        assert required in scopes, (
            f"defaultClientScopes lost the built-in {required!r} scope"
        )


def test_the_dev_org_alias_matches_the_seeded_db_alias() -> None:
    """alias -> org_id resolves through organization.kc_org_alias; the
    realm's alias and the seed's stamp must be the same string or every
    dev login resolves to nothing (403)."""
    from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

    assert DEFAULT_ORG_ALIAS == _DEV_ORG_ALIAS


def test_the_test_harness_org_claim_matches_the_realm() -> None:
    """authkit is the double for Keycloak: it must mint the same claim
    shape (a JSON array of alias strings) carrying the same alias the
    realm emits, or the suite validates tokens production never issues."""
    import jwt as pyjwt

    from tests.authkit import DEFAULT_ORG_ALIAS as KIT_ALIAS
    from tests.authkit import make_authkit

    assert KIT_ALIAS == _DEV_ORG_ALIAS
    _, mint = make_authkit()
    claims = pyjwt.decode(
        mint(roles=["accountant"]), options={"verify_signature": False}
    )
    # The pinned SHAPE: list of alias strings — the KC 26 default-mapper
    # output (multivalued, JSON type "String"; no ids, no attributes).
    assert claims["organization"] == [_DEV_ORG_ALIAS]
