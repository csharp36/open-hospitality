"""Encoded sick-leave rulesets, per wage jurisdiction (E4).

ONLY `US-CA` is encoded. `sick_leave_rules_for()` REFUSES anything else — a
non-CA property accruing zero silently would be indistinguishable from a
working engine with no accruals, the same refusal posture as
`overtime_rules.rules_for`.

Every figure traces to `docs/reference/ca-sick-leave.md` (researched
2026-07-20 against Cal. Labor Code §246 as amended by SB 616, and the DIR
FAQ last updated 2025-12-31). Encoded law goes stale: the ruleset carries
its citation and a re-check date, and consumers should warn past that date
naming the reference doc.

The caps implement the DIR's "whichever is more" reading: the days-based
figure (5 days usage / 10 days accrual x the employee's day length) can
RAISE the hours-based floors (40 / 80), never reduce them. A flat 40/80
engine under-credits anyone whose regular day exceeds 8 hours — the DIR's
own example is a 10-hour/day employee with a 50-hour usage floor.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


class UnknownSickLeaveJurisdiction(ValueError):
    """No encoded sick-leave ruleset for this jurisdiction. Refused rather
    than defaulted: silence and compliance are not the same thing."""


@dataclass(frozen=True)
class SickLeaveRules:
    jurisdiction: str
    # 1 hour accrued per this many hours worked (CA: 30).
    hours_worked_per_accrued_hour: Decimal
    # Statutory hour floors for the two caps; the days multipliers can only
    # raise them (usage_cap_hours / accrual_cap_hours).
    usage_cap_floor_hours: Decimal
    usage_cap_days: Decimal
    accrual_cap_floor_hours: Decimal
    accrual_cap_days: Decimal
    # Exempt staff are deemed this many worked hours per week for accrual
    # (DIR guidance; unless their normal week is fewer — see the plan).
    exempt_deemed_weekly_hours: Decimal
    usage_cap_window: str  # "calendar_year" — the recorded employer choice
    citation: str
    reference_doc: str
    recheck_by: date

    def usage_cap_hours(self, *, day_length: Decimal) -> Decimal:
        """Max sick hours usable per calendar year: 'five days or 40 hours,
        whichever is more' for this employee's day length."""
        return max(self.usage_cap_floor_hours, self.usage_cap_days * day_length)

    def accrual_cap_hours(self, *, day_length: Decimal) -> Decimal:
        """Max total accrued balance: 'ten days or 80 hours, whichever is
        more' for this employee's day length."""
        return max(
            self.accrual_cap_floor_hours, self.accrual_cap_days * day_length
        )


_CALIFORNIA = SickLeaveRules(
    jurisdiction="US-CA",
    hours_worked_per_accrued_hour=Decimal("30"),
    usage_cap_floor_hours=Decimal("40"),
    usage_cap_days=Decimal("5"),
    accrual_cap_floor_hours=Decimal("80"),
    accrual_cap_days=Decimal("10"),
    exempt_deemed_weekly_hours=Decimal("40"),
    usage_cap_window="calendar_year",
    citation="Cal. Labor Code §246 (as amended by SB 616, eff. 2024-01-01)",
    reference_doc="docs/reference/ca-sick-leave.md",
    recheck_by=date(2027, 1, 1),
)


def sick_leave_rules_for(jurisdiction: str) -> SickLeaveRules | None:
    """The ruleset, None, or a refusal — three DIFFERENT answers:

    - `US-CA`: the encoded ruleset.
    - `US`: None — the FLSA floor mandates NO paid sick leave, so zero
      accrual under plain `US` is encoded law, not an accident. Explicit,
      because 'no entries appeared' must never be ambiguous between 'correct'
      and 'broken'.
    - anything else: REFUSED. Most states with their own codes (NV, CO, …)
      have leave statutes this module has not encoded; accruing zero for
      them would be silently unlawful.
    """
    if jurisdiction == "US-CA":
        return _CALIFORNIA
    if jurisdiction == "US":
        return None
    raise UnknownSickLeaveJurisdiction(
        f"no encoded sick-leave ruleset for jurisdiction {jurisdiction!r}; "
        "refusing to accrue rather than silently accruing nothing. See "
        "docs/reference/ca-sick-leave.md for the encoding standard."
    )
