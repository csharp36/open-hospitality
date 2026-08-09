"""Overtime engine (Pillar B3) — PURE, no database.

Classifies each business date's worked hours into regular (1×), overtime (1.5×),
and double-time (2×) under a JURISDICTION'S rules, computed per WORKWEEK:

- Daily: hours over `rules.daily_ot_after` are OT; over `daily_dt_after` are DT.
  Most jurisdictions have neither and are weekly-only.
- Weekly: regular hours over `weekly_ot_after` are OT (the "no pyramiding" rule —
  hours already counted as daily OT/DT are NOT recounted toward the threshold).
- 7th consecutive day worked in a workweek, where the jurisdiction has such a
  rule: the first `seventh_day_dt_after` hours are OT and the rest DT (this
  overrides the daily classification for that day).
- Exempt employees (position.flsa_exempt) are excluded from OT entirely.

The rules come in as an `OvertimeRules` value rather than being baked in — see
usali.overtime_rules, which refuses unknown jurisdictions rather than defaulting
to California. Meal-break premiums are NOT priced here —
B2 raises them as timecard warnings; pricing is Pillar C. See the Pillar B design.

The workweek is a fixed, recurring 7-day period anchored to the same Monday as
the biweekly payroll period, so a 14-day timecard is exactly two workweeks —
which is what makes weekly OT computable per timecard.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from usali.overtime_rules import OvertimeRules

_WORKWEEK_DAYS = 7
_ZERO = Decimal("0")


@dataclass(frozen=True)
class DayOvertime:
    business_date: date
    regular_hours: Decimal
    ot_hours: Decimal  # paid at 1.5×
    dt_hours: Decimal  # paid at 2×


def _workweek_start(d: date, anchor: date) -> date:
    index = (d - anchor).days // _WORKWEEK_DAYS
    return anchor + timedelta(days=index * _WORKWEEK_DAYS)


def compute_overtime(
    day_hours: Mapping[date, Decimal],
    *,
    anchor: date,
    exempt: bool,
    rules: OvertimeRules,
) -> list[DayOvertime]:
    """Per-day regular/OT/DT hours for a set of worked business dates.

    `day_hours` maps a business date to that day's worked hours (lunch already
    excluded, from B2's engine). `anchor` is the Monday the workweek/period grid
    is aligned to. `rules` is the ruleset for the jurisdiction the work was
    performed in. Returns one `DayOvertime` per input date, chronologically.
    """
    if exempt:
        return [
            DayOvertime(d, day_hours[d], _ZERO, _ZERO) for d in sorted(day_hours)
        ]

    weeks: dict[date, list[date]] = {}
    for d in day_hours:
        weeks.setdefault(_workweek_start(d, anchor), []).append(d)

    out: list[DayOvertime] = []
    for wk_start in sorted(weeks):
        days = sorted(weeks[wk_start])
        # The 7th consecutive day worked in the workweek == all 7 days worked
        # (the workweek is a fixed 7-day window). This is the pilot-correct
        # simplification; a partial-week gap means no 7th-day premium.
        seventh = (
            days[-1]
            if rules.seventh_day_ot and len(days) == _WORKWEEK_DAYS
            else None
        )
        daily_ot_after = rules.daily_ot_after
        daily_dt_after = rules.daily_dt_after

        classified: dict[date, list[Decimal]] = {}  # date -> [regular, ot, dt]
        for d in days:
            h = day_hours[d]
            if d == seventh and rules.seventh_day_dt_after is not None:
                ot = min(rules.seventh_day_dt_after, h)
                dt = max(h - rules.seventh_day_dt_after, _ZERO)
                reg = _ZERO
            elif daily_ot_after is None:
                # Weekly-only jurisdiction: the whole day is regular here, and
                # the weekly pass below converts anything past the threshold.
                reg, ot, dt = h, _ZERO, _ZERO
            elif daily_dt_after is None:
                reg = min(daily_ot_after, h)
                ot = max(h - daily_ot_after, _ZERO)
                dt = _ZERO
            else:
                reg = min(daily_ot_after, h)
                ot = min(max(h - daily_ot_after, _ZERO), daily_dt_after - daily_ot_after)
                dt = max(h - daily_dt_after, _ZERO)
            classified[d] = [reg, ot, dt]

        # Weekly-40 pass over REGULAR hours only (no pyramiding). Regular hours
        # beyond a cumulative 40 convert to OT, attributed to the day that
        # crossed the line.
        cumulative_regular = _ZERO
        weekly_after = rules.weekly_ot_after
        for d in days:
            reg, ot, dt = classified[d]
            if weekly_after is None:
                continue
            room = max(_ZERO, weekly_after - cumulative_regular)
            keep = min(reg, room)
            converted = reg - keep
            cumulative_regular += reg
            classified[d] = [keep, ot + converted, dt]

        out.extend(
            DayOvertime(d, classified[d][0], classified[d][1], classified[d][2])
            for d in days
        )

    return out
