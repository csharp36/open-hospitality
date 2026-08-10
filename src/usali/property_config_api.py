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

Writes gate on `require_grants(ORG_ADMIN, PROPERTY_GM)` composed with
`_require_onboardable_property` (org_admin bypass; a GM confined to assigned
properties), and each write emits exactly one `AuditEvent`. The two upserts
(POST inventory, PUT fiscal-calendar) are implemented as an ORM
get-or-update rather than a Core `pg_insert(...).on_conflict_do_update(...)`:
automatic org_id stamping happens in the ORM `before_flush` hook, and a Core
insert bypasses that hook, so on an org != 1 session the row would fall back
to the column server-default org_id=1 and RLS `WITH CHECK` would then reject
the write — the same "Core insert bypasses the stamp" gap the K-pillar F3
finding fixed for ingestion.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import (
    ORG_ADMIN,
    PROPERTY_GM,
    Principal,
    request_session_factory,
    require_grants,
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
    CALENDAR_TYPES,
    OOO_REASON_CODES,
    AuditEvent,
    FiscalCalendar,
    OutOfOrderRoom,
    RoomInventory,
)
from usali.workforce import (
    _require_onboardable_property,
    _require_readable_property,
    resolve_scope,
)

require_config_writer = require_grants(ORG_ADMIN, PROPERTY_GM)

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


# ---- write models ------------------------------------------------------


class InventoryBody(BaseModel):
    effective_date: date
    total_rooms: int = Field(gt=0)


class OooBody(BaseModel):
    start_date: date
    end_date: date
    room_count: int = Field(gt=0)
    reason_code: str
    note: str | None = None


class FiscalBody(BaseModel):
    calendar_type: str
    fiscal_year_start_month: int = Field(ge=1, le=12)
    week_start_weekday: int | None = Field(default=None, ge=0, le=6)


def _audit(session: Session, principal: Principal, action: str, property_id: str) -> None:
    session.add(AuditEvent(actor_subject=principal.subject, action=action,
                           resource_type="property", resource_id=property_id))


@router.post("/{property_id}/inventory", status_code=201)
def set_inventory(
    property_id: str, body: InventoryBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> InventoryRow:
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        # Upsert on (property_id, effective_date): a re-POST corrects the
        # count. ORM get-or-update (NOT a Core pg_insert) so the org_id
        # stamp applies via the before_flush hook on the INSERT path.
        row = session.execute(
            select(RoomInventory).where(
                RoomInventory.property_id == property_id,
                RoomInventory.effective_date == body.effective_date,
            )
        ).scalar_one_or_none()
        if row is None:
            row = RoomInventory(property_id=property_id, effective_date=body.effective_date,
                                total_rooms=body.total_rooms)
            session.add(row)
        else:
            row.total_rooms = body.total_rooms
        session.flush()  # populate row.inventory_id for the response
        _audit(session, principal, "property_inventory_set", property_id)
        session.commit()
        return InventoryRow(inventory_id=row.inventory_id, effective_date=row.effective_date,
                            total_rooms=row.total_rooms)


@router.post("/{property_id}/out-of-order", status_code=201)
def add_ooo(
    property_id: str, body: OooBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> OooRow:
    if body.end_date < body.start_date:
        raise HTTPException(status_code=422, detail="end_date must not precede start_date")
    if body.reason_code not in OOO_REASON_CODES:
        raise HTTPException(status_code=422,
                            detail=f"reason_code must be one of {sorted(OOO_REASON_CODES)}")
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        block = OutOfOrderRoom(property_id=property_id, start_date=body.start_date,
                               end_date=body.end_date, room_count=body.room_count,
                               reason_code=body.reason_code, note=body.note)
        session.add(block)
        session.flush()
        _audit(session, principal, "ooo_added", property_id)
        session.commit()
        return OooRow(ooo_id=block.ooo_id, start_date=block.start_date, end_date=block.end_date,
                      room_count=block.room_count, reason_code=block.reason_code, note=block.note)


@router.delete("/{property_id}/out-of-order/{ooo_id}", status_code=204)
def remove_ooo(
    property_id: str, ooo_id: int, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> None:
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        block = session.execute(
            select(OutOfOrderRoom).where(OutOfOrderRoom.ooo_id == ooo_id,
                                         OutOfOrderRoom.property_id == property_id)
        ).scalar_one_or_none()
        if block is None:
            raise HTTPException(status_code=404, detail="out-of-order block not found")
        session.delete(block)
        _audit(session, principal, "ooo_removed", property_id)
        session.commit()


@router.put("/{property_id}/fiscal-calendar")
def set_fiscal_calendar(
    property_id: str, body: FiscalBody, request: Request,
    principal: Principal = Depends(require_config_writer),
) -> FiscalConfigModel:
    if body.calendar_type not in CALENDAR_TYPES:
        raise HTTPException(status_code=422,
                            detail=f"calendar_type must be one of {sorted(CALENDAR_TYPES)}")
    is_445 = body.calendar_type == "445"
    if is_445 and body.week_start_weekday is None:
        raise HTTPException(status_code=422, detail="4-4-5 requires week_start_weekday")
    if not is_445 and body.week_start_weekday is not None:
        raise HTTPException(status_code=422,
                            detail="week_start_weekday only applies to a 4-4-5 calendar")
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        # Upsert on the property_id PK: ORM get-or-update, same reasoning
        # as set_inventory above — a Core pg_insert bypasses the org_id
        # before_flush stamp.
        row = session.get(FiscalCalendar, property_id)
        if row is None:
            row = FiscalCalendar(property_id=property_id, calendar_type=body.calendar_type,
                                 fiscal_year_start_month=body.fiscal_year_start_month,
                                 week_start_weekday=body.week_start_weekday)
            session.add(row)
        else:
            row.calendar_type = body.calendar_type
            row.fiscal_year_start_month = body.fiscal_year_start_month
            row.week_start_weekday = body.week_start_weekday
        _audit(session, principal, "fiscal_calendar_set", property_id)
        session.commit()
        return FiscalConfigModel(calendar_type=body.calendar_type,
                                 fiscal_year_start_month=body.fiscal_year_start_month,
                                 week_start_weekday=body.week_start_weekday)
