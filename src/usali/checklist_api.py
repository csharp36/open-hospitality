"""The onboarding checklist router (Track B/B4).

Its own module rather than more weight on `portal_api` (past 1200 lines).
Reading the checklist needs only the router's operator gate; DISMISSING an
item requires `org_admin`, because "we don't use payroll" is a standing
commitment about the tenant rather than a per-user preference.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from usali.auth import ORG_ADMIN, Principal, request_session_factory, require_grants
from usali.checklist import ITEMS, ChecklistItem, evaluate, summarize
from usali.models import OrgChecklistOverride
from usali.tenancy import current_org_id

router = APIRouter(prefix="/api/checklist")


def _session(request: Request) -> Session:
    return request_session_factory(request)()


class ItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    description: str
    required: bool
    where: str
    status: str
    detail: str | None = None


class ChecklistModel(BaseModel):
    items: list[ItemModel]
    open_count: int
    error_count: int
    all_clear: bool


@router.get("")
def get_checklist(request: Request) -> ChecklistModel:
    """Every registered item with its DERIVED status for the active org."""
    with _session(request) as session:
        rows = evaluate(session)
    summary = summarize(rows)
    items = [ItemModel(**vars(row)) for row in rows]
    return ChecklistModel(
        items=items, open_count=summary.open_count,
        error_count=summary.error_count, all_clear=summary.all_clear,
    )


require_checklist_admin = require_grants(ORG_ADMIN)

_BY_KEY = {item.key: item for item in ITEMS}


class DismissRequest(BaseModel):
    # Bounded to the column width (String(200)); an over-long note is a clean
    # 422 rather than an unhandled Postgres StringDataRightTruncation 500.
    note: str | None = Field(default=None, max_length=200)


def _item_or_404(key: str) -> ChecklistItem:
    item = _BY_KEY.get(key)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown checklist item")
    return item


@router.put("/{key}/dismissal", status_code=204)
def dismiss(
    key: str,
    request: Request,
    body: DismissRequest | None = None,
    principal: Principal = Depends(require_checklist_admin),
) -> Response:
    """Record that this tenant is opting out of an OPTIONAL item.

    Idempotent (D-B4.5): concurrent browser sessions must not collide on the
    composite key, so a repeat is ON CONFLICT DO NOTHING — which also keeps
    the FIRST dismisser's audit row, the decision that actually happened.
    """
    item = _item_or_404(key)
    if item.required:
        raise HTTPException(
            status_code=422,
            detail=f"{key} is required and cannot be dismissed",
        )
    with _session(request) as session:
        session.execute(
            pg_insert(OrgChecklistOverride)
            .values(
                org_id=current_org_id(session),
                item_key=key,
                note=(body.note if body else None),
                created_by=principal.subject,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "item_key"])
        )
        session.commit()
    return Response(status_code=204)


@router.delete("/{key}/dismissal", status_code=204)
def undismiss(
    key: str,
    request: Request,
    principal: Principal = Depends(require_checklist_admin),
) -> Response:
    """Reopen a dismissed item. Deleting an absent override is a no-op."""
    _item_or_404(key)
    with _session(request) as session:
        session.execute(
            delete(OrgChecklistOverride).where(OrgChecklistOverride.item_key == key)
        )
        session.commit()
    return Response(status_code=204)
