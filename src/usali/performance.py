"""Core performance statistics (issue #9): occupancy, ADR, RevPAR, TRevPAR, and
labor productivity, recomputed from primitives over a date range or a fiscal
period, with prior-period/prior-year comparisons and operator trend bases.

Pure functions over the promoted fact tables. Room/revenue metrics carry no
per-employee money and are ungated; labor-COST metrics compose with the
reporting._discloses per-day guard (never a fresh SUM) so a caller-controlled
window cannot be a differencing oracle. Denominators come from
inventory.rooms_available (fail-loud). #26 adds the expense side (GOPPAR/CPOR).
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import UsaliSegmentFact, UsaliStatisticFact


def _stat_by_day(
    session: Session, property_id: str, start: date, end: date, metric_code: str
) -> dict[date, Decimal]:
    """A promoted DAY statistic per business date (last write wins on a dup, as
    statistics are as-of KPIs, never summed — the _rooms_by_day convention)."""
    rows = session.execute(
        select(UsaliStatisticFact.business_date, UsaliStatisticFact.value).where(
            UsaliStatisticFact.property_id == property_id,
            UsaliStatisticFact.business_date >= start,
            UsaliStatisticFact.business_date <= end,
            UsaliStatisticFact.metric_code == metric_code,
            UsaliStatisticFact.period == "DAY",
            UsaliStatisticFact.is_prior_year.is_(False),
        )
    ).all()
    return {d: Decimal(str(v)) for d, v in rows}


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


_COMP_HOUSE_SEGMENTS = ("COMPLIMENTARY", "HOUSE_USE")  # promote stores canonical kinds


class AdrBasisUnavailable(Exception):
    """`exclude_comp_house` was requested but a day in the window has no segment
    data to net comp/house-use from — refuse rather than silently not-excluding
    (adr-010)."""


def _comp_house_rooms_by_day(
    session: Session, property_id: str, start: date, end: date
) -> dict[date, Decimal]:
    rows = session.execute(
        select(UsaliSegmentFact.business_date, UsaliSegmentFact.rooms).where(
            UsaliSegmentFact.property_id == property_id,
            UsaliSegmentFact.business_date >= start,
            UsaliSegmentFact.business_date <= end,
            UsaliSegmentFact.period == "DAY",
            UsaliSegmentFact.usali_segment.in_(_COMP_HOUSE_SEGMENTS),
        )
    ).all()
    out: dict[date, Decimal] = {}
    for d, rooms in rows:
        out[d] = out.get(d, Decimal("0")) + Decimal(str(rooms))
    return out


def _segment_days(
    session: Session, property_id: str, start: date, end: date
) -> set[date]:
    rows = session.execute(
        select(UsaliSegmentFact.business_date).where(
            UsaliSegmentFact.property_id == property_id,
            UsaliSegmentFact.business_date >= start,
            UsaliSegmentFact.business_date <= end,
            UsaliSegmentFact.period == "DAY",
        ).distinct()
    ).scalars().all()
    return set(rows)


def adr_rooms_sold(
    session: Session, property_id: str, start: date, end: date, basis: str
) -> Decimal:
    """Rooms sold on the ADR basis over the window. `as_reported` = Σ
    ROOMS_OCCUPIED; `exclude_comp_house` subtracts segment comp+house-use rooms,
    refusing (AdrBasisUnavailable) if any occupied day lacks segment data."""
    rooms = _stat_by_day(session, property_id, start, end, "ROOMS_OCCUPIED")
    total = sum(rooms.values(), Decimal("0"))
    if basis == "as_reported":
        return total
    seg_days = _segment_days(session, property_id, start, end)
    for d in rooms:
        if rooms[d] > 0 and d not in seg_days:
            raise AdrBasisUnavailable(
                f"{property_id} is set to exclude comp/house-use from ADR, but "
                f"{d.isoformat()} has occupied rooms and no market-segment data to "
                "net them from — ingest the segment statistics or switch the ADR basis"
            )
    comp_house = _comp_house_rooms_by_day(session, property_id, start, end)
    return total - sum(comp_house.values(), Decimal("0"))
