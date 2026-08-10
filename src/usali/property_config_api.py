"""Property configuration endpoints: room inventory, out-of-order rooms, and
fiscal calendar (issue #8).

Auth mirrors POST /api/departments: reads gate on `_require_readable_property`,
writes on `require_grants(ORG_ADMIN, PROPERTY_GM)` + `_require_onboardable_property`
(org_admin bypass; a GM confined to assigned properties). Every write emits one
AuditEvent; a refusal that passed confinement audits with a rollback first, so
the audit commit never sweeps in a partial write (the crm_api idiom).

Fail-loud reads: an unconfigured fiscal calendar, or a rooms-available window
reaching before the first inventory row, returns 409 with a named message
rather than a guess (adr-010).

This module currently implements the READ endpoints only (config,
rooms-available, fiscal-periods); the write endpoints + audit land in a
follow-up task.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import (
    Principal,
    request_session_factory,
    require_operator,
)
from usali.fiscal import (
    FiscalCalendarNotConfigured,
    FiscalConfig,
    period_containing,
    periods_in_year,
    require_config,
    resolve_period,
)
from usali.inventory import InventoryNotConfigured, rooms_available
from usali.models import (
    FiscalCalendar,
    OutOfOrderRoom,
    RoomInventory,
)
from usali.workforce import (
    _require_readable_property,
    resolve_scope,
)

router = APIRouter(prefix="/api/properties")


def _session(request: Request) -> Session:
    return request_session_factory(request)()


def _fiscal_config(session: Session, property_id: str) -> FiscalConfig | None:
    row = session.get(FiscalCalendar, property_id)
    if row is None:
        return None
    return FiscalConfig(
        calendar_type=row.calendar_type,
        fiscal_year_start_month=row.fiscal_year_start_month,
        week_start_weekday=row.week_start_weekday,
    )


# ---- read models -----------------------------------------------------------

class InventoryRow(BaseModel):
    inventory_id: int
    effective_date: date
    total_rooms: int


class OooRow(BaseModel):
    ooo_id: int
    start_date: date
    end_date: date
    room_count: int
    reason_code: str
    note: str | None


class FiscalConfigModel(BaseModel):
    calendar_type: str
    fiscal_year_start_month: int
    week_start_weekday: int | None


class ConfigResponse(BaseModel):
    property_id: str
    inventory: list[InventoryRow]
    out_of_order: list[OooRow]
    fiscal_calendar: FiscalConfigModel | None


@router.get("/{property_id}/config")
def get_config(
    property_id: str, request: Request,
    principal: Principal = Depends(require_operator),
) -> ConfigResponse:
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        inv = session.execute(
            select(RoomInventory).where(RoomInventory.property_id == property_id)
            .order_by(RoomInventory.effective_date.desc())
        ).scalars().all()
        ooo = session.execute(
            select(OutOfOrderRoom).where(OutOfOrderRoom.property_id == property_id)
            .order_by(OutOfOrderRoom.start_date.desc())
        ).scalars().all()
        cfg = _fiscal_config(session, property_id)
        return ConfigResponse(
            property_id=property_id,
            inventory=[InventoryRow(inventory_id=r.inventory_id, effective_date=r.effective_date,
                                    total_rooms=r.total_rooms) for r in inv],
            out_of_order=[OooRow(ooo_id=b.ooo_id, start_date=b.start_date, end_date=b.end_date,
                                 room_count=b.room_count, reason_code=b.reason_code, note=b.note)
                          for b in ooo],
            fiscal_calendar=None if cfg is None else FiscalConfigModel(
                calendar_type=cfg.calendar_type,
                fiscal_year_start_month=cfg.fiscal_year_start_month,
                week_start_weekday=cfg.week_start_weekday),
        )


class RoomsAvailableResponse(BaseModel):
    property_id: str
    start: date
    end: date
    room_nights: int


@router.get("/{property_id}/rooms-available")
def get_rooms_available(
    property_id: str, request: Request,
    start: date, end: date,
    principal: Principal = Depends(require_operator),
) -> RoomsAvailableResponse:
    if end < start:
        raise HTTPException(status_code=422, detail="end must not precede start")
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        try:
            nights = rooms_available(session, property_id, start, end)
        except InventoryNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return RoomsAvailableResponse(property_id=property_id, start=start, end=end,
                                      room_nights=nights)


class PeriodRow(BaseModel):
    key: str
    start: date
    end: date


@router.get("/{property_id}/fiscal-periods")
def get_fiscal_periods(
    property_id: str, request: Request,
    principal: Principal = Depends(require_operator),
    fiscal_year: int | None = None,
    period: str | None = None,
    on_date: Annotated[date | None, Query(alias="date")] = None,
) -> dict[str, list[dict[str, Any]]]:
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        try:
            cfg = require_config(_fiscal_config(session, property_id))
            if period is not None:
                start, end = resolve_period(cfg, period)
                return {"periods": [PeriodRow(key=period, start=start, end=end).model_dump()]}
            if on_date is not None:
                key = period_containing(cfg, on_date)
                start, end = resolve_period(cfg, key)
                return {"periods": [PeriodRow(key=key, start=start, end=end).model_dump()]}
            year = fiscal_year if fiscal_year is not None else date.today().year
            rows = [PeriodRow(key=k, start=s, end=e).model_dump()
                    for k, s, e in periods_in_year(cfg, year)]
            return {"periods": rows}
        except FiscalCalendarNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:  # malformed period key / out-of-range
            raise HTTPException(status_code=422, detail=str(exc)) from None
