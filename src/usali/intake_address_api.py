"""The property's night-audit email address, and the log of what arrived at it
(D-OH23.8): five operator routes under the property-config prefix.

WHY A MODULE OF ITS OWN. These routes sit under `/api/properties` beside
`property_config_api`'s and share its gates — `require_config_writer` is
imported from there rather than re-derived, so the two cannot drift into two
ideas of who may write a property's configuration. What they do NOT share is
the tenancy shape below, which is the reason they are not in that file.

TENANCY. `property_intake_address` carries `org_id` but NO RLS policy
(D-OH23.3: the webhook must resolve a local part before any org is known, and
a policy keyed on the request's org would refuse that lookup). So the wall on
this table is application code, here, twice over:

1. every route resolves the property first, through the org-bound `Property`
   lookup inside `_require_readable_property` / `_require_onboardable_property`
   — that table IS walled, so another tenant's property is refused as
   out of scope before this module reads anything; and
2. every SELECT and UPDATE of the address table carries
   `org_id == current_org_id(session)` explicitly — `_active_address` is the
   one place a live address is looked up, so there is one filter to keep
   rather than five.

`tests/test_intake_address_api.py::test_an_address_of_another_org_is_invisible_and_unrotatable`
holds both legs. `email_intake_event` IS OrgScoped and walled; its read here
still names `property_id` explicitly, because the org wall alone would show
every property in the org.

ONE LIVE ADDRESS PER PROPERTY IS THE DATABASE'S RULE.
`uq_property_intake_address_active` is a partial unique over the un-revoked
rows, so `create` does not read-then-write (two concurrent creates would both
pass such a check): it inserts and turns that constraint's IntegrityError into
the 409. The same index is why `rotate` flushes the revocation BEFORE adding
the new row — see the comment there.
"""

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from usali.auth import Principal, request_session_factory, require_operator
from usali.config import get_settings
from usali.intake import new_local_part
from usali.models import AuditEvent, EmailIntakeEvent, PropertyIntakeAddress
from usali.property_config_api import require_config_writer
from usali.tenancy import current_org_id
from usali.workforce import (
    _require_onboardable_property,
    _require_readable_property,
    resolve_scope,
)

router = APIRouter(prefix="/api/properties")

_ACTIVE_ADDRESS_CONSTRAINT = "uq_property_intake_address_active"

# A sender allowlist entry: a lowercase hostname of at least two labels
# (D-OH23.4 compares it with the envelope sender's domain, which always has
# one). Deliberately narrower than `intake._DOMAIN`, which parses what a
# receiver wrote; this is what an operator may type.
_SENDER_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_SENDER_DOMAIN = re.compile(rf"{_SENDER_LABEL}(?:\.{_SENDER_LABEL})+\Z")
_SENDER_DOMAIN_MAX_LEN = 253      # RFC 1035's limit on a fully qualified name
_MAX_SENDER_DOMAINS = 20

_EVENT_LIMIT_DEFAULT = 20
_EVENT_LIMIT_MAX = 100


def _session(request: Request) -> Session:
    return request_session_factory(request)()


def _active_address(session: Session, property_id: str) -> PropertyIntakeAddress | None:
    """The property's live address, or None — the ONE lookup of this table.

    The `org_id` predicate is this module's own wall (see the module
    docstring): `property_intake_address` has no RLS policy, so nothing below
    this line would filter it.
    """
    return session.scalars(
        select(PropertyIntakeAddress).where(
            PropertyIntakeAddress.org_id == current_org_id(session),
            PropertyIntakeAddress.property_id == property_id,
            PropertyIntakeAddress.revoked_at.is_(None),
        )
    ).one_or_none()


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """Whether this IntegrityError names `constraint`.

    psycopg carries the offending constraint in the error's diagnostics. A
    driver that does not is read as "some other constraint", so the error
    keeps travelling as a 500 rather than being answered with a conflict this
    route has not actually identified — which is also how that assumption is
    held: `tests/test_intake_address_api.py::
    test_a_second_create_is_refused_and_leaves_one_active_address` goes red
    with a 500 the moment the diagnostics stop naming the constraint.
    """
    diag = getattr(exc.orig, "diag", None)
    name: object = getattr(diag, "constraint_name", None)
    return name == constraint


def _audit(session: Session, principal: Principal, action: str, local_part: str) -> None:
    """One audit row per operator write, naming the address that was minted,
    rotated or re-scoped.

    `resource_id` is the LOCAL PART and not `property_id:local_part`:
    `audit_event.resource_id` is String(64), a local part is 29 characters
    (`intake.new_local_part`), and a signup-generated property id runs to 45
    (`mapping.property_registry._slugify` caps the slug at 40, plus a dash and
    four hex digits) — the pair does not fit, and Postgres answers an overflow
    with an error, not a truncation. The local part alone is unique
    (`uq_property_intake_address_local_part`) and its row names the property.
    The local part is displayable by D-OH23.3, not a secret hash, so writing
    it here discloses nothing the property page does not already show.
    """
    session.add(AuditEvent(
        actor_subject=principal.subject, action=action,
        resource_type="property_intake_address", resource_id=local_part,
    ))


# ---- the address -----------------------------------------------------------

class IntakeAddressModel(BaseModel):
    """The live address, or four nulls when the property has none. `address`
    is assembled here because the domain is a SETTING: the SPA must never
    hard-code it (D-OH23.8)."""

    address: str | None
    local_part: str | None
    created_at: datetime | None
    sender_domains: list[str] | None


class SenderDomainsBody(BaseModel):
    sender_domains: list[str]


_NO_ADDRESS = IntakeAddressModel(
    address=None, local_part=None, created_at=None, sender_domains=None
)


def _model(row: PropertyIntakeAddress) -> IntakeAddressModel:
    domain = get_settings().email_intake_domain
    return IntakeAddressModel(
        address=f"{row.local_part}@{domain}", local_part=row.local_part,
        created_at=row.created_at, sender_domains=row.sender_domains,
    )


def _require_active(session: Session, property_id: str) -> PropertyIntakeAddress:
    row = _active_address(session, property_id)
    if row is None:
        # 404 rather than an implicit create: with no address the property
        # page offers "Create address" (D-OH23.8), so a rotate or an
        # allowlist write here is a stale client, not a new intention.
        raise HTTPException(
            status_code=404, detail="this property has no active intake address"
        )
    return row


def _clean_sender_domains(entries: list[str]) -> list[str] | None:
    """The allowlist as it is stored: lowercase hostnames, deduplicated in the
    order given, or None for an empty list.

    None is "any authenticated sender" (D-OH23.4); an empty JSON array would
    be a second spelling of a policy that means something else entirely
    ("nobody"), and `intake.sender_allowed` reads only the NULL distinction.
    """
    if len(entries) > _MAX_SENDER_DOMAINS:
        raise HTTPException(
            status_code=422,
            detail=f"at most {_MAX_SENDER_DOMAINS} sender domains",
        )
    cleaned: list[str] = []
    for entry in entries:
        domain = entry.strip().lower()
        if len(domain) > _SENDER_DOMAIN_MAX_LEN:
            # The entry is not echoed: it is unbounded caller text and the
            # only thing wrong with it is its length.
            raise HTTPException(
                status_code=422,
                detail=f"a sender domain is at most {_SENDER_DOMAIN_MAX_LEN} characters",
            )
        if not _SENDER_DOMAIN.fullmatch(domain):
            raise HTTPException(
                status_code=422, detail=f"not a sender domain: {domain!r}"
            )
        if domain not in cleaned:
            cleaned.append(domain)
    return cleaned or None


@router.get("/{property_id}/intake-address")
def get_intake_address(
    property_id: str, request: Request,
    principal: Principal = Depends(require_operator),
) -> IntakeAddressModel:
    """The property's live night-audit address, gated like `get_config`."""
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        row = _active_address(session, property_id)
        return _NO_ADDRESS if row is None else _model(row)


@router.post("/{property_id}/intake-address", status_code=201)
def create_intake_address(
    property_id: str, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> IntakeAddressModel:
    """Mint the property's address. Created on demand, never at property
    creation: a property that uploads by hand should not carry an unused
    capability (D-OH23.8)."""
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        row = PropertyIntakeAddress(
            local_part=new_local_part(), org_id=current_org_id(session),
            property_id=property_id,
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            if not _violates(exc, _ACTIVE_ADDRESS_CONSTRAINT):
                raise
            # Not read-then-write: the partial unique is what makes "one live
            # address" true under concurrency, so it is also what answers here.
            raise HTTPException(
                status_code=409,
                detail="this property already has an active intake address; "
                       "rotate it instead of creating a second one",
            ) from exc
        model = _model(row)
        _audit(session, principal, "intake_address_created", row.local_part)
        session.commit()
        return model


@router.post("/{property_id}/intake-address/rotate", status_code=201)
def rotate_intake_address(
    property_id: str, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> IntakeAddressModel:
    """Revoke the live address and mint a new one, in one transaction.

    The new row starts with NO allowlist: a rotation mints a new capability,
    and carrying the old policy across would set a sender policy the operator
    did not choose for it.
    """
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        old = _require_active(session, property_id)
        old.revoked_at = datetime.now(UTC)
        # Flushed BEFORE the new row is added, so the UPDATE reaches the
        # database first: `uq_property_intake_address_active` counts un-revoked
        # rows and refuses an insert made while the old row is still live.
        # Ordering it here is the point — the order is this function's, not a
        # property of how one flush happens to sequence an UPDATE and an INSERT
        # against the same table.
        session.flush()
        new = PropertyIntakeAddress(
            local_part=new_local_part(), org_id=current_org_id(session),
            property_id=property_id,
        )
        session.add(new)
        session.flush()
        model = _model(new)
        _audit(session, principal, "intake_address_rotated", new.local_part)
        session.commit()
        return model


@router.put("/{property_id}/intake-address")
def set_sender_domains(
    property_id: str, body: SenderDomainsBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> IntakeAddressModel:
    """Narrow the live address to these envelope-sender domains, or (with an
    empty list) back to any authenticated sender — D-OH23.4."""
    domains = _clean_sender_domains(body.sender_domains)
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        row = _require_active(session, property_id)
        row.sender_domains = domains
        session.flush()
        model = _model(row)
        _audit(session, principal, "intake_sender_domains_set", row.local_part)
        session.commit()
        return model


# ---- the event log ---------------------------------------------------------

class IntakeEventModel(BaseModel):
    """One received message. `auth_result` is deliberately absent: it is the
    receiver's free-text Authentication-Results, up to 1000 characters of
    detail an operator cannot act on, and `outcome` already carries the
    decision it fed (`sender_rejected`)."""

    event_id: int
    received_at: datetime
    envelope_from: str
    subject: str | None
    outcome: str
    message_id: str | None
    # As stored (D-OH23.7): [{name, sha256, bytes, outcome, batch_id?, error?}].
    attachments: list[dict[str, object]]


class IntakeEventsResponse(BaseModel):
    events: list[IntakeEventModel]


@router.get("/{property_id}/intake-events")
def list_intake_events(
    property_id: str, request: Request,
    limit: Annotated[int, Query(ge=1, le=_EVENT_LIMIT_MAX)] = _EVENT_LIMIT_DEFAULT,
    principal: Principal = Depends(require_operator),
) -> IntakeEventsResponse:
    """The property's recent intake events, newest first. Gated like
    `get_config`; `event_id` breaks a `received_at` tie so two messages
    recorded in the same transaction still list in the order they arrived."""
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        rows = session.scalars(
            select(EmailIntakeEvent)
            .where(EmailIntakeEvent.property_id == property_id)
            .order_by(EmailIntakeEvent.received_at.desc(), EmailIntakeEvent.event_id.desc())
            .limit(limit)
        ).all()
        return IntakeEventsResponse(events=[
            IntakeEventModel(
                event_id=row.event_id, received_at=row.received_at,
                envelope_from=row.envelope_from, subject=row.subject,
                outcome=row.outcome, message_id=row.message_id,
                attachments=row.attachments,
            )
            for row in rows
        ])
