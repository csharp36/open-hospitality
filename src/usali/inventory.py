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


class InventoryInconsistent(Exception):
    """Out-of-order rooms exceed the rooms in force on some day, so the day's
    availability would be negative — physically impossible. We refuse loudly
    (adr-010) rather than hand #9 a negative denominator; a re-POST of the
    inventory or a correction to the out-of-order blocks resolves it."""


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
    Σ_day max(0, in-force total − Σ_block-covering-day room_count).

    Computed per day rather than as one total minus one OOO sum so that (a) a
    day whose out-of-order rooms exceed inventory is caught at that day and
    refused loudly (`InventoryInconsistent`) instead of silently dragging the
    window negative, and (b) overlapping blocks that together exceed inventory
    can never manufacture a negative denominator for #9. A day fully out of
    service is a legitimate 0; only a would-be-negative day refuses.
    """
    if end < start:
        raise ValueError("end must not precede start")
    rows = _inventory_rows(session, property_id)
    blocks = list(
        session.execute(
            select(OutOfOrderRoom).where(
                OutOfOrderRoom.property_id == property_id,
                OutOfOrderRoom.start_date <= end,
                OutOfOrderRoom.end_date >= start,
            )
        ).scalars()
    )

    total = 0
    day = start
    while day <= end:
        in_force = _in_force(rows, day)  # raises if any day is unconfigured
        ooo_today = sum(
            b.room_count for b in blocks if b.start_date <= day <= b.end_date
        )
        net = in_force - ooo_today
        if net < 0:
            raise InventoryInconsistent(
                f"out-of-order rooms ({ooo_today}) exceed the {in_force} rooms in "
                f"force on {day.isoformat()} for {property_id} — availability would be "
                "negative; correct the room count or the out-of-order blocks"
            )
        total += net
        day += timedelta(days=1)
    return total
