"""Business-date attribution for punches (Pillar B1).

A punch is attributed to a business date in the PROPERTY'S local timezone with a
cutoff hour (default 04:00): a punch before the cutoff belongs to the PRIOR
business date. This deliberately aligns labor with the PMS night-audit business
date the revenue facts already use — otherwise an overnight housekeeping or
night-audit shift lands labor on a different day than the revenue it supported,
and the P&L is subtly wrong.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def business_date_for(
    punched_at: datetime, timezone: str, cutoff_hour: int = 4
) -> date:
    """The business date a timezone-aware `punched_at` belongs to."""
    if punched_at.tzinfo is None:
        raise ValueError("punched_at must be timezone-aware")
    local = punched_at.astimezone(ZoneInfo(timezone))
    if local.hour < cutoff_hour:
        return (local - timedelta(days=1)).date()
    return local.date()
