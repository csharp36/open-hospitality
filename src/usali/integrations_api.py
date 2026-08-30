"""The per-tenant integration connect surface (OH-17).

Its own module rather than more weight on `portal_api` (past 1200 lines), the
call `checklist_api` already made. Every route is org_admin: connecting a
tenant's accounting system is a standing commitment about the TENANT, the same
reasoning that gates checklist dismissal — and the READ is gated too, because
the identifiers name the tenant's external accounts and the list of what is
NOT connected is a map of the workspace's gaps.

NO SECRET IS EVER RETURNED. The read echoes only the non-secret identifiers
(realm, company id, client id) — being able to see WHICH QBO company a tenant
is pointed at is the value of the read surface; being able to read the token
back is only a liability. Re-entering a key is how you change it. This is
ADR-004's blind-read posture applied to a store the server can technically
decrypt (ADR-005), so nothing but this module's discretion enforces it: the
guard is `test_no_secret_is_ever_on_the_wire`, which greps the whole body.

VERIFY BEFORE PERSIST (D-OH17.8): the checklist probe is a cheap presence
check, so a row that cannot authenticate would be a `done` over an integration
that 502s on first use — the drift D-B4.1 and D8.3 exist to prevent. The
write path is where that is stopped, by making one live provider call.

REFUSAL ORDERING. Authorization is the only `Depends`-resolved refusal here;
everything else — unknown integration, unknown provider, missing field, a
provider that says no — is decided inside the handler, AFTER the gate has
answered. That is not stylistic. A dependency runs before the handler, so an
integration-shaped refusal wired as a dependency outruns the 403 and answers
an out-of-scope caller with tenant state; this branch has already shipped that
bug once. Nor may any refusal become an existence oracle: an unknown
integration under an unauthorized caller is a 403, never a 404.
"""

import hashlib
import hmac
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from usali.auth import ORG_ADMIN, Principal, request_session_factory, require_grants
from usali.config import Settings, get_settings
from usali.crm_feed import CrmFeedError
from usali.crypto import oauth_state_key
from usali.integrations import (
    ACCOUNTING,
    ALL_CREDENTIAL_FIELDS,
    INTEGRATIONS,
    CannotVerify,
    CredentialUnreadable,
    credential_for,
    spec_for,
)
from usali.models import AuditEvent, OrgIntegrationCredential, Property
from usali.payroll_provider import ProviderError
from usali.qbo_client import QboError
from usali.tenancy import OrgBoundSessionFactory, current_org_id

router = APIRouter(prefix="/api/integrations")

# The Intuit callback ONLY. Its own router because `create_app` includes
# `router` above with `operator_gates`, and the callback must be included with
# NONE — see the `callback` docstring. Splitting the routers is what makes
# "ungated" a property of the MOUNT rather than a comment nobody enforces.
callback_router = APIRouter(prefix="/api/integrations")

require_integration_admin = require_grants(ORG_ADMIN)

# What a failed verification can raise. Each adapter's own error type, so a
# provider failure is a 422 while a genuine bug still surfaces as a 500.
# `CannotVerify` is deliberately NOT in this tuple: it is not the provider
# refusing, and its message is already the operator-facing one.
_VERIFY_ERRORS = (CrmFeedError, ProviderError, QboError)


def _session(request: Request) -> Session:
    return request_session_factory(request)()


class IntegrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration: str
    connected: bool
    provider: str | None
    identifiers: dict[str, str]
    connected_at: str | None


class IntegrationsModel(BaseModel):
    items: list[IntegrationModel]


def _integration_or_404(integration: str) -> str:
    """The three keys are a CLOSED set (`INTEGRATIONS`, mirrored by the DB
    CHECK), identical for every tenant — so a 404 here discloses nothing about
    THIS tenant. It is still only reachable past the org_admin gate."""
    if integration not in INTEGRATIONS:
        raise HTTPException(status_code=404, detail="unknown integration")
    return integration


@router.get("")
def get_integrations(
    request: Request,
    principal: Principal = Depends(require_integration_admin),
) -> IntegrationsModel:
    """Every integration, connected or not, with its provider and non-secret
    identifiers. Never a secret — see this module's docstring."""
    del principal  # the gate is the point; the read is not attributed
    items: list[IntegrationModel] = []
    with _session(request) as session:
        # Built INSIDE the session: `credential_for` returns ORM instances,
        # and reading their columns after the session closes works only for
        # as long as nothing expires them. Not a risk worth carrying for the
        # two lines it would save.
        for integration in INTEGRATIONS:
            try:
                row = credential_for(session, integration)
            except CredentialUnreadable as exc:
                # ADR-005: a rotated `field_encryption_key` makes the row
                # undecryptable, and loading it decrypts every secret column
                # at once — so this page is where an operator meets the
                # rotation. Refusing the WHOLE page, by name, beats rendering
                # the readable integrations and this one as `connected:
                # false`: that is the lie `CredentialUnreadable` exists to
                # prevent, and it would be told on the very surface someone
                # came to for an explanation. The remedy the message names
                # still works while this refuses — `connect` below upserts
                # without ever reading the old row.
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if row is None:
                items.append(IntegrationModel(
                    integration=integration, connected=False, provider=None,
                    identifiers={}, connected_at=None,
                ))
                continue
            spec = spec_for(integration, row.provider)
            # A row whose pair is unknown cannot happen — the DB CHECK refuses
            # it — but reading `spec.plain_fields` off None would be a 500
            # rather than a legible answer if it ever did.
            plain = spec.plain_fields if spec is not None else ()
            items.append(IntegrationModel(
                integration=integration, connected=True, provider=row.provider,
                identifiers={
                    field: value
                    for field in plain
                    if (value := getattr(row, field)) is not None
                },
                connected_at=row.connected_at.isoformat(),
            ))
    return IntegrationsModel(items=items)


class ConnectRequest(BaseModel):
    # extra="allow": the credential fields differ per provider, and they are
    # validated against the provider's spec below rather than by a union of
    # five models. An unknown field is refused there, so nothing is smuggled
    # through — the check is just later and gives a better message. The cost
    # is that pydantic types NONE of the extras, which is why `connect`
    # checks that every supplied value is a string before it goes near a
    # column.
    model_config = ConfigDict(extra="allow")

    provider: str


def _first_crm_ref(session: Session) -> str | None:
    """Any property in this org carrying a crm_ref. Every real CRM read is
    property-scoped, so verification needs one; WHICH property is immaterial,
    because the credential is org-wide. Ordered so that a support question
    about a failed verify has one answer and not a coin flip.

    Org scoping is the session's, not this query's: `Property` is `OrgScoped`,
    so both L2 walls confine the SELECT — there is no org_id to pass wrong."""
    return session.execute(
        select(Property.crm_ref)
        .where(Property.crm_ref.is_not(None))
        .order_by(Property.property_id)
        .limit(1)
    ).scalar_one_or_none()


def _verify(
    request: Request, integration: str, provider: str, values: dict[str, Any]
) -> None:
    """One live provider call, so a credential that cannot authenticate never
    becomes a row (D-OH17.8). Injected in tests via app.state.verify_integration.

    The session it opens for the crm_ref is closed before the outbound call:
    a provider that hangs must not hold a database connection (and a
    transaction) open for the length of its timeout."""
    with _session(request) as session:
        crm_ref = _first_crm_ref(session)
    request.app.state.verify_integration(integration, provider, values, crm_ref)


def _store_credential(
    session: Session,
    integration: str,
    provider: str,
    supplied: dict[str, Any],
    spec_fields: tuple[str, ...],
    subject: str,
) -> None:
    """Upsert the tenant's row for one integration. Does NOT commit — the
    caller owns the transaction, so the write and its `AuditEvent` land
    together or not at all.

    Every field this provider does not use is explicitly nulled: a stale
    api_key surviving a switch from Tripleseat to Delphi is exactly what the
    CHECK's "must be NULL" half refuses, and PUT is a full replace.

    The conflict target is the composite PRIMARY KEY, and `org_id` in the
    inserted values comes from the session's own context — so the UPDATE arm
    can only ever reach this org's row. Do not "simplify" the conflict target
    to `integration` alone: a Core UPDATE carries no org_id of its own, and
    `tenancy._stamp_wall` covers ORM INSERTs only, so it would be confined by
    RLS and nothing else (which a superuser connection bypasses).
    """
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = dict.fromkeys(ALL_CREDENTIAL_FIELDS, None)
    values.update({field: supplied[field] for field in spec_fields})
    session.execute(
        pg_insert(OrgIntegrationCredential)
        .values(
            org_id=current_org_id(session), integration=integration,
            provider=provider, connected_at=now, connected_by=subject,
            **values,
        )
        .on_conflict_do_update(
            index_elements=["org_id", "integration"],
            set_={
                "provider": provider, "connected_at": now,
                "connected_by": subject, **values,
            },
        )
    )


def _audit(session: Session, subject: str, action: str, integration: str) -> None:
    """Takes the SUBJECT, not a `Principal`: the OAuth callback has no
    principal to hand over (no bearer token reaches it — D-OH17.11) and its
    actor comes out of the verified `state` instead. One audit shape for all
    four verbs rather than a second `session.add(AuditEvent(...))` that could
    drift on `resource_type`."""
    session.add(AuditEvent(actor_subject=subject, action=action,
                           resource_type="integration", resource_id=integration))


@router.put("/{integration}", status_code=204)
def connect(
    integration: str,
    body: ConnectRequest,
    request: Request,
    principal: Principal = Depends(require_integration_admin),
) -> Response:
    """Connect (or re-connect) one integration. A full replace, and refused
    unless the credentials actually authenticate."""
    _integration_or_404(integration)
    spec = spec_for(integration, body.provider)
    if spec is None:
        # The PAIR, never the provider alone: 'qbo' is legal under
        # 'accounting' and nowhere else — the rule the DB CHECK enforces.
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider!r} is not a provider for {integration}",
        )
    supplied = body.model_dump(exclude={"provider"})
    missing = [f for f in spec.fields if not supplied.get(f)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider} needs {', '.join(sorted(missing))}",
        )
    unknown = [f for f in supplied if f not in spec.fields]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider} does not take {', '.join(sorted(unknown))}",
        )
    mistyped = [f for f, v in supplied.items() if not isinstance(v, str)]
    if mistyped:
        # `extra="allow"` leaves the credential values un-typed, so without
        # this a number lands on a String column as a psycopg error and a
        # nested object lands in the EncryptedString bind processor — both
        # 500s on a caller mistake that deserves a 422.
        raise HTTPException(
            status_code=422,
            detail=f"{', '.join(sorted(mistyped))} must be a string",
        )
    try:
        _verify(request, integration, body.provider, supplied)
    except CannotVerify as exc:
        # Not the provider refusing — nothing was asked, because nothing
        # COULD be. The message is already operator-facing (it names the
        # missing crm_ref, or the OAuth flow), so it is passed through whole
        # rather than wrapped in a "rejected these credentials" that would
        # misattribute the refusal.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except _VERIFY_ERRORS as exc:
        # The provider's own message only — these adapters are built never to
        # put a response body in an exception (crm_feed.CrmFeedError,
        # payroll_provider.ProviderError both say so), so this cannot leak
        # one. The single deliberate exception is Gusto's verify(), whose
        # request carries no PII precisely so its detail can be shown.
        raise HTTPException(
            status_code=422,
            detail=f"{body.provider} rejected these credentials: {exc}",
        ) from exc

    with _session(request) as session:
        _store_credential(
            session, integration, body.provider, supplied, spec.fields,
            principal.subject,
        )
        _audit(session, principal.subject, "integration_connected", integration)
        session.commit()
    return Response(status_code=204)


@router.delete("/{integration}", status_code=204)
def disconnect(
    integration: str,
    request: Request,
    principal: Principal = Depends(require_integration_admin),
) -> Response:
    """Disconnect one integration. Deleting an absent row is a 204 no-op,
    matching `checklist_api.undismiss`: a repeat DELETE from a second browser
    tab is not an error, and a 404 would make absence observable.

    The DELETE carries no org_id, exactly as `checklist_api` does: the ORM
    read wall is SELECT-only, but the `org_wall` RLS policy has no `FOR`
    clause, so it covers DELETE too — the business key alone is tenant-safe
    on the serving (non-superuser) role."""
    _integration_or_404(integration)
    with _session(request) as session:
        session.execute(
            delete(OrgIntegrationCredential).where(
                OrgIntegrationCredential.integration == integration
            )
        )
        _audit(session, principal.subject, "integration_disconnected", integration)
        session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# The QBO OAuth pair (OH-17 Task 11, D-OH17.10/D-OH17.11)
#
# `connect` above REFUSES 'qbo' (CannotVerify): Intuit rotates the refresh
# token on every grant, so a pasted one cannot be checked without spending it
# and leaving the stored copy dead. Completing the consent flow is therefore
# the ONLY way `accounting` can be connected, and these two routes are what
# make that checklist item closeable at all.
# --------------------------------------------------------------------------

_QBO = "qbo"

# How long a signed `state` stays valid. Ten minutes is a consent screen plus
# an Intuit login plus a fumbled password — long enough that no honest
# operator meets an expiry, short enough that a state captured from a browser
# history, a proxy log or a Referer is dead before it can be used. It is not a
# session lifetime: nothing legitimate holds one of these across a coffee.
_STATE_TTL_SECONDS = 600

# Where the callback lands the operator when the grant completes. A SPA route,
# so the browser that followed Intuit's redirect ends up back in the connect
# UI with the result visible rather than looking at a JSON body.
_CONNECTED_REDIRECT = "/integrations?connected=accounting"


def qbo_redirect_uri(settings: Settings) -> str:
    """The one redirect URI, for both the consent request and the code
    exchange.

    ONE function because Intuit compares the two byte-for-byte and answers a
    mismatch with `invalid_grant` at exchange time — long after the consent
    URL looked perfectly fine — so two f-strings that "obviously" agree are a
    bug waiting for someone to add a trailing slash to one of them.

    Built from `public_base_url` and NEVER from the request. A request's Host
    (or X-Forwarded-Host) is attacker-controlled behind a proxy, and a
    redirect_uri derived from it would ask Intuit to deliver the tenant's
    authorization code to the attacker's domain. `public_base_url` exists for
    exactly this class of problem (it already backs the signup links).

    This is deliberately NOT a setting of its own: a second knob is a second
    thing to get out of sync with the value registered in the Intuit app
    dashboard, and the path half is ours, not the deployment's.
    """
    return f"{settings.public_base_url}/api/integrations/accounting/callback"


def sign_state(*, org_id: int, subject: str, now: float | None = None) -> str:
    """`org_id:subject:expiry:hmac` (D-OH17.11).

    The callback has no bearer token and no active-org header, so this string
    is the ONLY carrier of "which tenant is this grant for". Everything the
    callback is allowed to do is derived from what verifies out of here.

    Deliberately NOT single-use against a nonce store. Replaying a WHOLE
    callback is already dead: the other half of it is Intuit's `code`, which
    is single-use AT INTUIT, so a replayed state necessarily carries a spent
    code and the token exchange refuses it.

    That is the full extent of what it buys, and the limit is ACCEPTED
    (2026-08-30), not overlooked: a captured, unexpired state paired with the
    attacker's OWN fresh code binds THEIR realm and refresh token onto the
    victim org's row. Do not "fix" this by adding the nonce store — it refuses
    only the second use, and an attacker who calls back before the admin
    consumes the nonce himself, so it narrows the window without closing it.
    The fix, if this is ever revisited, is a browser-bound cookie set at
    `authorize` and required here. D-OH17.11's residual-risk block in the
    OH-17 design doc carries the full reasoning.

    The MAC covers `org_id`, `subject` AND `expiry` together — not the org
    alone. An unsigned expiry is no expiry, and an unsigned subject lets a
    grant be attributed to anyone.
    """
    expiry = int((now if now is not None else time.time()) + _STATE_TTL_SECONDS)
    payload = f"{org_id}:{subject}:{expiry}"
    mac = hmac.new(oauth_state_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{mac}"


def verify_state(state: str) -> tuple[int, str] | None:
    """(org_id, subject), or None for anything wrong.

    ONE None for every failure mode on purpose — forged, expired, malformed
    and missing must be indistinguishable to the caller, or the refusal
    becomes an oracle about other tenants' in-flight grants. That is also why
    nothing in here raises: a ValueError would surface as a 500 whose shape
    tells the caller which check it tripped.

    The MAC is checked FIRST and everything else afterwards, so no parse of
    attacker-controlled bytes happens until the signature has vouched for
    them.
    """
    payload, separator, mac = state.rpartition(":")
    if not separator:
        return None
    expected = hmac.new(
        oauth_state_key(), payload.encode(), hashlib.sha256
    ).hexdigest()
    # compare_digest, never ==: a timing-variable comparison on a MAC is the
    # textbook forgery oracle. Compared as BYTES because `state` is
    # attacker-supplied and `hmac.compare_digest` raises TypeError on a str
    # holding non-ASCII — which would be a 500 distinguishable from this
    # function's single refusal.
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return None
    # Past this point the bytes are ours: the split below cannot be steered,
    # because any re-split of a signed payload re-serialises to that same
    # signed string.
    org_raw, _, rest = payload.partition(":")
    subject, _, expiry_raw = rest.rpartition(":")
    try:
        if int(expiry_raw) < time.time():
            return None
        return int(org_raw), subject
    except ValueError:
        return None


class AuthorizeUrlModel(BaseModel):
    url: str


@router.get("/accounting/authorize")
def authorize(
    request: Request,
    principal: Principal = Depends(require_integration_admin),
) -> AuthorizeUrlModel:
    """The Intuit consent URL for the ACTIVE org.

    Returns the URL rather than 302-ing: the SPA navigates the top-level
    window itself, so the fetch seam in `api/client.ts` and its one-shot
    `redirectToLogin` latch are never asked to follow a cross-origin redirect.

    Gated exactly like every other route here, and that matters more than it
    looks: this endpoint MINTS the signatures the ungated callback trusts. An
    org_admin gate here is what stops anyone from obtaining a valid `state`
    for an org they do not administer without having to forge one.
    """
    settings = get_settings()
    with _session(request) as session:
        # The org comes from the request's validated active org — the same
        # binding every other route writes under — and is then sealed into
        # the state. Nothing later re-derives it.
        org_id = current_org_id(session)
        _audit(session, principal.subject, "integration_authorize_started", ACCOUNTING)
        session.commit()
    params = urlencode({
        "client_id": settings.qbo_client_id,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "redirect_uri": qbo_redirect_uri(settings),
        "state": sign_state(org_id=org_id, subject=principal.subject),
    })
    return AuthorizeUrlModel(url=f"{settings.qbo_authorize_url}?{params}")


@callback_router.get("/accounting/callback")
def callback(
    request: Request,
    code: str | None = Query(default=None),
    realm_id: str | None = Query(default=None, alias="realmId"),
    state: str | None = Query(default=None),
) -> Response:
    """Complete the grant and store the tenant's realm + refresh token.

    Mounted OUTSIDE the operator gates (`server.create_app` includes
    `callback_router` with no dependencies): this arrives as a top-level
    browser navigation with no bearer token and no active-org header, so
    `require_operator` and `require_active_org` would both refuse it. All of
    its authorization therefore comes from the signed `state` — which is why
    the signature and the TTL are load-bearing here rather than defence in
    depth, and why the org-bound session below is built from the org INSIDE
    the state and from nothing else. Do NOT "helpfully" read an org from a
    query parameter, a header or a cookie here: any of those is a
    cross-tenant credential injection with no forgery required at all.

    Every parameter is OPTIONAL in the signature and refused in the body on
    purpose. Declared required, a missing `state` would be FastAPI's 422
    naming the field — a refusal no other failure mode produces, and so an
    oracle distinguishing "you sent nothing" from "your state did not
    verify".

    Nothing is written until Intuit has honoured the code, so a refused grant
    leaves no row — the same verify-before-persist rule D-OH17.8 puts on the
    paste path, arrived at here for free because the exchange IS the
    verification.
    """
    verified = verify_state(state or "")
    if verified is None:
        raise HTTPException(status_code=400, detail="invalid authorization state")
    org_id, subject = verified
    if not code or not realm_id:
        # Only reachable by a caller holding a VALID state, so naming what is
        # missing discloses nothing. Real Intuit sends `error=access_denied`
        # here when the operator declines consent.
        raise HTTPException(
            status_code=400, detail="QuickBooks returned no authorization code"
        )
    try:
        refresh_token: str = request.app.state.exchange_qbo_code(code)
    except QboError as exc:
        # The client never puts a response body in a QboError, so this cannot
        # leak Intuit's payload; the status and Intuit's own fault message are
        # what an operator needs to tell "you declined" from "that code is
        # already spent".
        raise HTTPException(
            status_code=400, detail=f"QuickBooks refused the grant: {exc}"
        ) from exc

    # The spec, never a literal field list: `_store_credential` nulls every
    # column this provider does not use, which is what stops a previous
    # provider's secret surviving a re-connect. `spec_for` is Optional in the
    # type system only — ('accounting', 'qbo') is in PROVIDERS and mirrored by
    # the DB CHECK — so this branch exists to refuse loudly rather than store
    # a half-nulled row if PROVIDERS ever loses the pair.
    spec = spec_for(ACCOUNTING, _QBO)
    if spec is None:  # pragma: no cover - PROVIDERS would have to drop qbo
        raise HTTPException(
            status_code=500, detail="accounting/qbo is not a known provider pair"
        )

    # The org-bound factory is built from the VERIFIED org and the app's
    # UNBOUND base factory — the callback has no `request.state.session_factory`
    # (that is `require_active_org`'s doing, and it did not run). Both L2 walls
    # then confine the write, and `_store_credential` takes its `org_id` from
    # this session's context, so the upsert's UPDATE arm can only ever reach
    # this org's row.
    factory = OrgBoundSessionFactory(request.app.state.db_session_factory, org_id)
    with factory() as session:
        _store_credential(
            session, ACCOUNTING, _QBO,
            {"realm_id": realm_id, "refresh_token": refresh_token},
            spec.fields, subject,
        )
        # The actor is the subject sealed into the state — the operator who
        # started the grant — because there is no principal on this request.
        _audit(session, subject, "integration_connected", ACCOUNTING)
        session.commit()
    # No secret and no code on this redirect: it travels through the browser's
    # history and every proxy in between (the module docstring's rule, applied
    # to the one route that holds a freshly minted token).
    return RedirectResponse(url=_CONNECTED_REDIRECT, status_code=307)
