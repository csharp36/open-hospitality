"""Effective-dated room inventory and rooms-available (issue #8).

Pure query functions over `room_inventory` + `out_of_order_room`. The count in
force for a date is the greatest-`effective_date`-<=-date row. Rooms available
over an inclusive window is the per-day sum of in-force counts minus OOO
room-nights (each block clamped to the window). Fail-loud: a window reaching a
date with no in-force inventory row refuses rather than inventing a denominator
(adr-010). #9 (core performance statistics) divides by this number.
"""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import OutOfOrderRoom, RoomInventory


class InventoryNotConfigured(Exception):
    """No room-inventory row is in force for a queried date — the count is
    unknown, so we refuse rather than guess a denominator."""


def _inventory_rows(session: Session, property_id: str) -> list[RoomInventory]:
    return list(
        session.execute(
            select(RoomInventory)
            .where(RoomInventory.property_id == property_id)
            .order_by(RoomInventory.effective_date)
        ).scalars()
    )


def _in_force(rows: list[RoomInventory], day: date) -> int:
    in_force: int | None = None
    for row in rows:  # rows ascending by effective_date
        if row.effective_date <= day:
            in_force = row.total_rooms
        else:
            break
    if in_force is None:
        raise InventoryNotConfigured(
            f"no room inventory in force on {day.isoformat()} — set an effective-dated "
            "room count on or before this date before computing availability"
        )
    return in_force


def total_rooms_on(session: Session, property_id: str, day: date) -> int:
    """The sellable-room count in force for `property_id` on `day`."""
    return _in_force(_inventory_rows(session, property_id), day)


def rooms_available(session: Session, property_id: str, start: date, end: date) -> int:
    """Room-nights available over the inclusive window [start, end]:
    Σ_day(in-force total) − Σ_block(overlap_days × room_count)."""
    if end < start:
        raise ValueError("end must not precede start")
    rows = _inventory_rows(session, property_id)

    total = 0
    day = start
    while day <= end:
        total += _in_force(rows, day)  # raises if any day is unconfigured
        day += timedelta(days=1)

    blocks = session.execute(
        select(OutOfOrderRoom).where(
            OutOfOrderRoom.property_id == property_id,
            OutOfOrderRoom.start_date <= end,
            OutOfOrderRoom.end_date >= start,
        )
    ).scalars()
    for block in blocks:
        overlap_start = max(block.start_date, start)
        overlap_end = min(block.end_date, end)
        nights = (overlap_end - overlap_start).days + 1
        total -= nights * block.room_count
    return total
