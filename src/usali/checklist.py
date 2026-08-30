"""The onboarding open-items checklist (Track B/B4, D8.2).

Status is DERIVED, never stored: each item owns a `probe(session) -> bool`
that answers "is this configured for the active org?" against the caller's
already-org-bound session, so both tenancy walls apply and a probe cannot
observe another tenant's rows. The only persisted state is a dismissal
(`OrgChecklistOverride`), and a dismissal LOSES to a probe that says done
(D-B4.4) — an operator who dismissed payroll in August and connected it in
March must see `done`, not a stale "dismissed".

`ITEMS` is the closed set, the `CRM_PROVIDERS` idiom: one place to read the
whole checklist. Its keys are mirrored by a literal CHECK on
`org_checklist_override.item_key` (models.py) so the DB refuses an unknown
key independently of this import.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import OrgChecklistOverride

logger = logging.getLogger("usali.checklist")

Probe = Callable[[Session], bool]

DONE = "done"
OPEN = "open"
DISMISSED = "dismissed"
ERROR = "error"


@dataclass(frozen=True)
class ChecklistItem:
    key: str
    title: str
    description: str
    required: bool
    where: str  # the SPA route that closes this item
    probe: Probe


@dataclass(frozen=True)
class ItemStatus:
    key: str
    title: str
    description: str
    required: bool
    where: str
    status: str
    detail: str | None = None


def evaluate(
    session: Session, items: Sequence[ChecklistItem] | None = None
) -> list[ItemStatus]:
    """One ItemStatus per registered item, computed under `session`'s org."""
    registry = ITEMS if items is None else items
    dismissed = {
        key for (key,) in session.execute(select(OrgChecklistOverride.item_key))
    }
    out: list[ItemStatus] = []
    for item in registry:
        try:
            done = item.probe(session)
        except Exception as exc:  # design §8: loud, but contained
            logger.exception("checklist probe failed for %s", item.key)
            out.append(_status(item, ERROR, detail=type(exc).__name__))
            continue
        if done:
            status = DONE
        elif item.key in dismissed:
            status = DISMISSED
        else:
            status = OPEN
        out.append(_status(item, status))
    return out


def _status(item: ChecklistItem, status: str, *, detail: str | None = None) -> ItemStatus:
    return ItemStatus(
        key=item.key, title=item.title, description=item.description,
        required=item.required, where=item.where, status=status, detail=detail,
    )


ITEMS: tuple[ChecklistItem, ...] = ()  # filled in Task 4
