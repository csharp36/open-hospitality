"""The onboarding checklist router (Track B/B4).

Its own module rather than more weight on `portal_api` (past 1200 lines).
Reading the checklist needs only the router's operator gate; DISMISSING an
item requires `org_admin`, because "we don't use payroll" is a standing
commitment about the tenant rather than a per-user preference.
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from usali.auth import request_session_factory
from usali.checklist import OPEN, evaluate

router = APIRouter(prefix="/api/checklist")


def _session(request: Request) -> Session:
    return request_session_factory(request)()


class ItemModel(BaseModel):
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
    all_clear: bool


@router.get("")
def get_checklist(request: Request) -> ChecklistModel:
    """Every registered item with its DERIVED status for the active org."""
    with _session(request) as session:
        rows = evaluate(session)
    items = [ItemModel(**vars(row)) for row in rows]
    open_count = sum(1 for row in rows if row.status == OPEN)
    return ChecklistModel(
        items=items, open_count=open_count, all_clear=open_count == 0
    )
