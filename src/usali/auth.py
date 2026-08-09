"""OIDC resource-server auth for the platform (Pillar A1).

Validates Keycloak-issued JWTs and exposes FastAPI dependencies that gate
routes. The signing-key lookup is injected (`signing_key_resolver`) so tests
mint tokens with a local keypair — no Keycloak needed in the test loop. In
production, `TokenVerifier.from_settings()` backs the resolver with PyJWT's
`PyJWKClient`, which fetches and caches the realm JWKS.
"""

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient, PyJWKClientConnectionError
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.config import Settings
from usali.tenancy import ActiveOrgSessionFactory, SessionFactory

# Absorbs Keycloak <-> resource-server clock drift at the exp/iat boundary.
_LEEWAY_SECONDS = 60

# Hosts for which cleartext-http JWKS fetching is tolerated (local dev only).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class AuthError(Exception):
    """A bearer token was missing, malformed, expired, or not trusted."""


class AuthUnavailableError(AuthError):
    """JWKS/issuer temporarily unreachable — maps to 503 upstream."""

    # Subclass of AuthError so existing AuthError handlers still catch it.
    # Task 4's require_auth maps this to 503 while plain AuthError -> 401.


@dataclass(frozen=True)
class Principal:
    subject: str
    username: str
    roles: frozenset[str]
    # (property_id, department_id|None) pairs from the token's `scopes` claim;
    # None = claim absent → resolve_scope falls back to the role_assignment DB.
    scopes: frozenset[tuple[str, int | None]] | None = None
    # Keycloak organization memberships from the token's `organization`
    # claim (L3, decision 3): org ALIASES, never ids — the DB resolves
    # alias -> org_id, nothing numeric from a token is ever an org_id.
    # KC 26's organization-membership mapper at its default config
    # (multivalued, JSON type "String") emits a JSON array of alias
    # strings; that one shape is pinned in tests/test_oidc_realm_contract
    # and anything else refuses via _malformed. None = claim absent (KC
    # omits the claim entirely for users in no organization).
    org_aliases: frozenset[str] | None = None


class TokenVerifier:
    """Validates RS256 JWTs against an issuer, audience, and signing key."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        signing_key_resolver: Callable[[str], object],
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._resolve = signing_key_resolver

    @classmethod
    def from_settings(cls, settings: Settings) -> "TokenVerifier":
        jwks_url = settings.oidc_jwks_url
        parts = urlsplit(jwks_url)
        if parts.scheme != "https" and parts.hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                "refusing to fetch JWKS over cleartext http from a "
                f"non-loopback host: {jwks_url}"
            )
        jwk_client = PyJWKClient(jwks_url)

        def resolve(kid: str) -> object:
            return jwk_client.get_signing_key(kid).key

        return cls(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            signing_key_resolver=resolve,
        )

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
            key = self._resolve(header.get("kid", ""))
            claims = jwt.decode(
                token,
                key,  # type: ignore[arg-type]  # resolver returns an opaque key object (RSAPublicKey / PyJWK); PyJWT accepts it at runtime
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except PyJWKClientConnectionError as exc:
            # JWKS endpoint unreachable -> transient; distinct from a bad token.
            raise AuthUnavailableError(str(exc)) from exc
        except (jwt.PyJWTError, KeyError) as exc:
            # Covers InvalidTokenError AND PyJWKClientError (e.g. unknown/rotated
            # kid via the real client); KeyError is the injected-resolver contract.
            raise AuthError(str(exc)) from exc
        # Signature/issuer/audience are now trusted, but claim SHAPES are not —
        # Keycloak misconfiguration or a token minted by a trusted-but-buggy
        # client can carry structurally wrong claims. Any shape surprise must be
        # a 401 (AuthError), never an unhandled TypeError/KeyError -> 500.
        try:
            return _principal_from_claims(claims)
        except AuthError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise AuthError("malformed claims") from exc


def _malformed(claim: str) -> AuthError:
    # Name the offending claim only — never echo its value into errors or logs
    # (claim values are attacker-controlled).
    return AuthError(f"malformed claim: {claim}")


def _principal_from_claims(claims: dict[str, object]) -> Principal:
    """Extract a Principal, strictly validating claim shapes (AuthError on any
    deviation). `sub` presence is already enforced by jwt.decode(require=)."""
    realm_access = claims.get("realm_access", {})
    if not isinstance(realm_access, dict):
        raise _malformed("realm_access")
    roles = realm_access.get("roles", [])
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise _malformed("realm_access.roles")

    raw_scopes = claims.get("scopes")
    scopes: frozenset[tuple[str, int | None]] | None = None
    if raw_scopes is not None:
        if not isinstance(raw_scopes, list):
            raise _malformed("scopes")
        pairs: list[tuple[str, int | None]] = []
        for entry in raw_scopes:
            if not isinstance(entry, dict):
                raise _malformed("scopes")
            property_id = entry.get("property_id")
            if not isinstance(property_id, str) or not property_id:
                raise _malformed("scopes[].property_id")
            department_id = entry.get("department_id")
            if department_id is not None and (
                isinstance(department_id, bool) or not isinstance(department_id, int)
            ):
                raise _malformed("scopes[].department_id")
            pairs.append((property_id, department_id))
        scopes = frozenset(pairs)

    raw_orgs = claims.get("organization")
    org_aliases: frozenset[str] | None = None
    if raw_orgs is not None:
        # Exactly the shape the realm's mapper config emits (a JSON array
        # of alias strings) — a dict here would mean someone flipped the
        # mapper to "add organization id/attributes", and a silent
        # normalization would hide that drift until it mattered.
        if not isinstance(raw_orgs, list) or not all(
            isinstance(a, str) and a for a in raw_orgs
        ):
            raise _malformed("organization")
        org_aliases = frozenset(raw_orgs)

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise _malformed("sub")
    username = claims.get("preferred_username", sub)
    if not isinstance(username, str) or not username:
        raise _malformed("preferred_username")
    return Principal(
        subject=sub,
        username=username,
        roles=frozenset(roles),
        scopes=scopes,
        org_aliases=org_aliases,
    )


# A1 realm roles. The property/department SCOPING of these is A2; here they are
# a coarse gate. Any operator role may reach the financial portal; `employee`
# (self-service only) may not.
ORG_ADMIN = "org_admin"
ACCOUNTANT = "accountant"
PROPERTY_GM = "property_gm"
DEPARTMENT_MANAGER = "department_manager"
EMPLOYEE = "employee"
PAYROLL_ADMIN = "payroll_admin"
OPERATOR_ROLES: frozenset[str] = frozenset(
    {ORG_ADMIN, ACCOUNTANT, PROPERTY_GM, DEPARTMENT_MANAGER, PAYROLL_ADMIN}
)
# The operator roles whose authority is ORG-WIDE, not tied to one property:
# their grant carries property_id NULL (the l4a0orggrant shape), and
# resolve_scope reads that as all-properties. The seeds/provision_tenant have
# always written these org-wide; since L6b the API onboarding path does too, so
# an onboarded org_admin and a seeded/provisioned one share ONE grant shape.
# property_gm / department_manager are deliberately absent — their authority IS
# a place, so their grant stays property/department-scoped.
ORG_WIDE_ROLES: frozenset[str] = frozenset({ORG_ADMIN, ACCOUNTANT, PAYROLL_ADMIN})

_bearer = HTTPBearer(auto_error=False)


def _verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier


def require_auth(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return _verifier(request).verify(creds.credentials)
    except AuthUnavailableError as exc:
        raise HTTPException(
            status_code=503, detail="auth temporarily unavailable"
        ) from exc
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_roles(*allowed: str) -> Callable[[Principal], Principal]:
    """TOKEN-role gate. Since L4 (Pillar L decision 4) realm token roles
    are only the coarse operator/employee door — realm roles are
    realm-GLOBAL, so they can never again decide org authority. The one
    remaining production use is `require_operator`; every org-gated
    surface composes `require_grants` on top instead."""
    allowed_set = frozenset(allowed)

    def _dep(principal: Principal = Depends(require_auth)) -> Principal:
        if principal.roles.isdisjoint(allowed_set):
            raise HTTPException(status_code=403, detail="insufficient role")
        return principal

    return _dep


require_operator = require_roles(*OPERATOR_ROLES)


def effective_roles(session: Session, subject: str) -> frozenset[str]:
    """The subject's roles IN THE ORG the session is bound to (Pillar L
    decision 4): the distinct roles of their `role_assignment` grants,
    read through the org-bound request session — both L2 walls confine
    the read, so grants held in another org are simply not visible. The
    session MUST be an org-bound one (a request session); an unbound
    instrumented session refuses loudly rather than answering globally.
    """
    from usali.models import RoleAssignment

    return frozenset(
        session.execute(
            select(RoleAssignment.role).distinct().where(
                RoleAssignment.keycloak_subject == subject
            )
        ).scalars()
    )


def require_grants(*allowed: str) -> Callable[..., Principal]:
    """DB-grant gate (decision 4): 403 unless the caller holds a
    `role_assignment` grant for one of `allowed` VISIBLE UNDER THE
    ACTIVE ORG'S RLS. Composes on the coarse token gate — an
    `employee` token stays refused even if grant rows exist for the
    subject (a stale row must not out-rank the realm's coarse claim),
    and an org_admin-of-A token active in org B finds no grant there.
    Refusal words match the token gate's exactly: which door refused
    is not the caller's business."""
    allowed_set = frozenset(allowed)

    def _dep(
        request: Request, principal: Principal = Depends(require_operator)
    ) -> Principal:
        factory = request_session_factory(request)
        with factory() as session:
            granted = effective_roles(session, principal.subject)
        if granted.isdisjoint(allowed_set):
            raise HTTPException(status_code=403, detail="insufficient role")
        return principal

    return _dep


# --------------------------------------------------------------- active org
# L3 (Pillar L decision 3): membership claim -> validated active org ->
# the session variable. The SPA names its active org by ALIAS in this
# header; the server validates active ∈ the token's memberships and the
# DB (organization.kc_org_alias) resolves alias -> org_id — nothing
# numeric from the token is ever an org_id.

ACTIVE_ORG_HEADER = "X-Active-Org"

# ONE refusal string for BOTH "alias the token is not a member of" and
# "alias no organization row answers to": distinguishing them would be
# an existence oracle over other tenants' aliases.
_ACTIVE_ORG_REFUSED = "active organization refused"


def _active_org_alias(principal: Principal, header_alias: str | None) -> str:
    """The validated active-org ALIAS for this request, or a refusal.

    Refusal semantics (decision 3): ambiguity refuses (400), never
    guesses; a non-member alias refuses (403) naming nothing; a token
    with no memberships has no org to act in (403) — the founding org is
    NOT a fallback for orgless tokens.
    """
    aliases = principal.org_aliases
    if not aliases:
        # None (claim absent — KC omits it for users in no organization)
        # and empty collapse to the same refusal on purpose.
        raise HTTPException(status_code=403, detail=_ACTIVE_ORG_REFUSED)
    if not header_alias:
        # '' treated as absent: an empty header names nothing.
        if len(aliases) == 1:
            return next(iter(aliases))
        raise HTTPException(
            status_code=400,
            detail=f"{ACTIVE_ORG_HEADER} header required: "
            "token carries multiple organization memberships",
        )
    if header_alias not in aliases:
        raise HTTPException(status_code=403, detail=_ACTIVE_ORG_REFUSED)
    return header_alias


def _unknown_alias_403() -> HTTPException:
    # Same status, same words as the non-member refusal — no oracle.
    return HTTPException(status_code=403, detail=_ACTIVE_ORG_REFUSED)


def require_active_org(
    request: Request, principal: Principal = Depends(require_auth)
) -> SessionFactory:
    """Dependency wiring decision 3 into the request: validates the
    active org against the token's memberships and stashes the
    org-bound session factory the routers read back through
    `request_session_factory`. Membership/ambiguity refusals (403/400)
    fire HERE, before any handler runs; the unknown-alias 403 (claim
    admits the alias, no organization row answers to it) fires at the
    request's FIRST session use, when the deferred alias -> org_id
    lookup runs.
    """
    alias = _active_org_alias(
        principal, request.headers.get(ACTIVE_ORG_HEADER)
    )
    factory = ActiveOrgSessionFactory(
        request.app.state.db_session_factory, alias,
        unknown_alias_error=_unknown_alias_403,
    )
    request.state.session_factory = factory
    return factory


class ActiveOrgNotResolved(RuntimeError):
    """A handler asked for the request's org-bound session factory but
    require_active_org never ran — the route was wired without the
    dependency (see create_app's operator_gates). Named so a
    misassembled router fails diagnosably, never as a bare
    AttributeError deep in a handler."""


def request_session_factory(request: Request) -> SessionFactory:
    """The org-bound session factory require_active_org stashed for this
    request — the ONE way handlers obtain request sessions."""
    factory = getattr(request.state, "session_factory", None)
    if factory is None:
        raise ActiveOrgNotResolved(
            "request has no org-bound session factory — the route was "
            "included without the require_active_org dependency "
            "(create_app wires it via operator_gates)"
        )
    return factory  # type: ignore[no-any-return]  # request.state is untyped
