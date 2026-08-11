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

from usali.models import UsaliStatisticFact


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
