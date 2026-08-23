"""PMS-interest capture (Track B/B1 Part-2). When a signing-up owner names a PMS
we don't support, we record a de-duped demand signal instead of dropping it.
NOT OrgScoped — an admin reads it across workspaces. Does NOT commit; the caller
owns the transaction (the signup path records it in the same request as
provisioning)."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import PmsInterestRequest


def _normalize(raw_pms: str) -> str:
    """De-dupe key: lowercase, strip everything non-alphanumeric, so 'HotelKey',
    'hotel key', and 'Hotel-Key!' all collapse to 'hotelkey'. Fuzzy/alias
    matching (e.g. 'cloud beds' ~ 'cloudbeds pms') is a later refinement."""
    return re.sub(r"[^a-z0-9]+", "", raw_pms.lower())


def record_request(
    session: Session, *, org_alias: str, email: str, raw_pms: str
) -> tuple[PmsInterestRequest, bool]:
    """Record a PMS-interest request, de-duped on (org_alias, normalized_pms).
    Returns (row, is_new). is_new is False when this (workspace, PMS) was already
    captured — the caller notifies the admin only on is_new."""
    normalized = _normalize(raw_pms)
    existing = session.execute(
        select(PmsInterestRequest).where(
            PmsInterestRequest.org_alias == org_alias,
            PmsInterestRequest.normalized_pms == normalized,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    row = PmsInterestRequest(
        org_alias=org_alias, email=email, raw_pms=raw_pms, normalized_pms=normalized,
    )
    session.add(row)
    session.flush()
    return row, True
