"""Which property's P&L carries each worked minute.

Hours attribute to the property whose KIOSK recorded the punch —
`Punch.kiosk_device_id → KioskDevice.property_id`, a link that has existed since
B1 and was never read. NOT to the employee's primary property, which is what
every pre-E1 caller assumed and which is wrong for the 21 of 28 pilot staff who
work both hotels.

## The unrecorded bucket

`compute_day` returns `property_minutes` keyed by property, with a `None` key
holding minutes whose property was never recorded. That only happens for
`TimecardAdjustment` rows written before E1 added `property_id`: a manager
correcting a missed clock-out recorded what and when, never where.

Those minutes are allocated **proportionally to the same day's device-derived
split** — if punches put 6h at SJCES and 5h at 58033, an unrecorded hour splits
6:5. Where a day has no device-derived split at all (every punch missing, the
whole day reconstructed by adjustment), they fall back to the employee's primary
assignment.

Both are ESTIMATES, and deliberately confined to this module so they cannot be
mistaken for recorded facts elsewhere. New adjustments carry a real
`property_id`, so this path shrinks toward nothing as pre-E1 rows age out.
"""

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from usali.apportion import apportion
from usali.assignments import primary_assignment_on
from usali.models import Timecard
from usali.timecards import compute_timecard

_MINUTES_PER_HOUR = Decimal("60")
_HOURS = Decimal("0.01")


class UnattributableHoursError(Exception):
    """Worked minutes exist that cannot be tied to any property.

    Raised rather than dropping them or parking them on an arbitrary property:
    silently discarded hours understate a hotel's labor cost, and silently
    misplaced hours overstate the wrong one. Both are invisible in the output.
    """


def property_minutes_for_day(
    session: Session,
    *,
    employee_id: int,
    business_date: date,
    property_minutes: dict[str | None, int],
) -> dict[str, int]:
    """Resolve one day's split, allocating any unrecorded minutes.

    `property_minutes` comes straight from `DayResult`. The return value has no
    `None` key and sums to the same total.
    """
    recorded = {k: v for k, v in property_minutes.items() if k is not None}
    unrecorded = property_minutes.get(None, 0)
    if unrecorded <= 0:
        return recorded

    if recorded:
        return _spread(unrecorded, recorded)

    # Nothing device-derived that day — the whole day came from adjustments.
    primary = primary_assignment_on(session, employee_id, business_date)
    if primary is None:
        raise UnattributableHoursError(
            f"employee {employee_id} worked {unrecorded} minutes on "
            f"{business_date.isoformat()} with no device-recorded property and no "
            "primary assignment in effect. Refusing to guess a property: the "
            "hours would land on the wrong hotel's Schedule 14 with no signal."
        )
    return {primary.property_id: unrecorded}


def _spread(extra: int, recorded: dict[str, int]) -> dict[str, int]:
    """Distribute `extra` across `recorded` in proportion, exactly."""
    shares = apportion(Decimal(extra), recorded, quantum=Decimal(1))
    return {k: v + int(shares[k]) for k, v in recorded.items()}


def property_hours_for_timecard(
    session: Session, card: Timecard
) -> dict[str, Decimal]:
    """Worked HOURS per property across the whole card.

    This is the allocation weight for labor cost. It is NOT how overtime is
    computed — OT runs on the employee's combined hours first (California
    overtime is per-employer, not per-work-location), and only the resulting
    cost is split using these weights. Splitting before the overtime pass would
    turn 6h + 5h into two sub-8-hour days and compute zero daily OT on an
    11-hour day.
    """
    totals: dict[str, int] = {}
    for day in compute_timecard(session, card):
        if day.worked_minutes <= 0:
            continue
        resolved = property_minutes_for_day(
            session,
            employee_id=card.employee_id,
            business_date=day.business_date,
            property_minutes=day.property_minutes,
        )
        for property_id, minutes in resolved.items():
            totals[property_id] = totals.get(property_id, 0) + minutes
    # Quantizing each property independently did NOT tie to the card: 50min +
    # 50min gave 0.83 + 0.83 = 1.66 against a true 1.6667. Apportion the exact
    # total instead, so the parts sum to the whole.
    total_hours = (
        Decimal(sum(totals.values())) / _MINUTES_PER_HOUR
    ).quantize(_HOURS)
    return apportion(total_hours, totals, quantum=_HOURS)
