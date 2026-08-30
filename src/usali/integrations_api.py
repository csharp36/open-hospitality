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

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from usali.auth import ORG_ADMIN, Principal, request_session_factory, require_grants
from usali.crm_feed import CrmFeedError
from usali.integrations import (
    ALL_CREDENTIAL_FIELDS,
    INTEGRATIONS,
    CannotVerify,
    credential_for,
    spec_for,
)
from usali.models import AuditEvent, OrgIntegrationCredential, Property
from usali.payroll_provider import ProviderError
from usali.qbo_client import QboError
from usali.tenancy import current_org_id

router = APIRouter(prefix="/api/integrations")

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
            row = credential_for(session, integration)
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


def _audit(session: Session, principal: Principal, action: str, integration: str) -> None:
    session.add(AuditEvent(actor_subject=principal.subject, action=action,
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
        _audit(session, principal, "integration_connected", integration)
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
        _audit(session, principal, "integration_disconnected", integration)
        session.commit()
    return Response(status_code=204)
