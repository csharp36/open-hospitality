"""Invite-gate service (Track B/B1, D-B4). The raw token is a bearer secret:
created once, returned once (for the emailed link), and stored only as its
SHA-256. Validation is fail-closed — unknown / expired / non-pending all return
None, and the caller refuses without an existence oracle.

These functions do NOT commit; the caller owns the transaction boundary (the
signup-completion path consumes the invite in the SAME transaction as
provision_tenant, so a provisioning failure leaves the invite pending)."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from usali.models import Invite

_DEFAULT_TTL = timedelta(days=7)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_invite(
    session: Session,
    email: str,
    *,
    ttl: timedelta = _DEFAULT_TTL,
    now: datetime | None = None,
) -> tuple[Invite, str]:
    """Create a pending invite for `email`. Returns (row, raw_token); the raw
    token is shown to the caller ONCE and never stored in the clear."""
    moment = now or _now()
    raw_token = secrets.token_urlsafe(32)
    invite = Invite(
        email=email,
        token_hash=_hash_token(raw_token),
        status="pending",
        expires_at=moment + ttl,
    )
    session.add(invite)
    session.flush()
    return invite, raw_token


def validate(
    session: Session, raw_token: str, *, now: datetime | None = None
) -> Invite | None:
    """The pending, unexpired invite whose hash matches `raw_token`, or None.
    Fail-closed on every miss — the caller refuses naming nothing."""
    moment = now or _now()
    invite = session.execute(
        select(Invite).where(Invite.token_hash == _hash_token(raw_token))
    ).scalar_one_or_none()
    if invite is None or invite.status != "pending":
        return None
    if invite.expires_at <= moment:
        return None
    return invite


def consume(session: Session, invite: Invite, org_id: int) -> None:
    """Mark the invite consumed and record which tenant it became."""
    invite.status = "consumed"
    invite.consumed_org_id = org_id
    session.flush()


def claim(session: Session, raw_token: str, *, now: datetime | None = None) -> bool:
    """Atomically transition a pending, unexpired invite to ``consumed`` in a
    single UPDATE. Returns True iff THIS call won the claim (exactly one row
    moved pending->consumed). Concurrent callers serialize on the row under
    READ COMMITTED, so at most one wins — this is the one-time-use gate for the
    signup-completion path: provisioning happens only for the winner. Does NOT
    set consumed_org_id (the org does not exist yet); record it with
    ``consume``/``mark_consumed_org`` after provisioning. The caller commits."""
    moment = now or _now()
    result = cast("CursorResult[Any]", session.execute(
        update(Invite)
        .where(
            Invite.token_hash == _hash_token(raw_token),
            Invite.status == "pending",
            Invite.expires_at > moment,
        )
        .values(status="consumed")
    ))
    return result.rowcount == 1


def revert_claim(session: Session, raw_token: str) -> None:
    """Undo a claim that did not result in a provisioned tenant (e.g. a
    provisioning failure), returning the invite to ``pending`` so it can be
    retried. Only reverts a still-org-less claim (consumed_org_id IS NULL), so
    it never resurrects a fully-completed signup. The caller commits."""
    session.execute(
        update(Invite)
        .where(
            Invite.token_hash == _hash_token(raw_token),
            Invite.status == "consumed",
            Invite.consumed_org_id.is_(None),
        )
        .values(status="pending")
    )


def mark_consumed_org(session: Session, raw_token: str, org_id: int) -> None:
    """Record which tenant a consumed invite became (audit). The claim already
    set status=consumed; this fills consumed_org_id after provisioning. The
    caller commits."""
    session.execute(
        update(Invite)
        .where(Invite.token_hash == _hash_token(raw_token))
        .values(consumed_org_id=org_id)
    )


def revoke(session: Session, invite: Invite) -> None:
    invite.status = "revoked"
    session.flush()
