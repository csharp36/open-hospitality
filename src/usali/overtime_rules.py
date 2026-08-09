"""Jurisdiction rulesets for the overtime engine.

WHY THIS EXISTS: `overtime.py` had California's thresholds as module constants.
That is correct for the pilot and wrong as a foundation — the engine is reached
by every costing path, and a second property in another state would have been
silently costed under California rules.

## The refusal is the point

`rules_for()` raises on an unknown jurisdiction. It does NOT fall back to
California. A Texas property costed under California daily overtime would
overstate labor on every Schedule 14 line and, if the numbers were ever used to
drive actual pay, would misstate wages — silently, because the arithmetic
succeeds. An outright failure at the first costing attempt is far cheaper than a
quarter of plausible-looking wrong numbers.

Consequently a jurisdiction appears in `_RULES` only once its rules have been
confirmed against a primary source (statute, wage order, or state labor
department publication) and encoded in tests. "Commonly stated on an HR blog" is
not sufficient. Adding a state is deliberately a small, evidenced change.

## Scope of what is modelled

Daily/weekly thresholds, double-time, and a seventh-consecutive-day premium
cover the shapes we have verified. Rules that do NOT fit this vocabulary —
Colorado's consecutive-hours provision, Nevada's wage-conditional daily rule,
meal/rest premiums — are deliberately absent rather than approximated. When such
a jurisdiction is needed, extend the vocabulary; do not bend an existing field
to nearly mean the right thing.
"""

from dataclasses import dataclass
from decimal import Decimal

_ZERO = Decimal("0")


class UnknownJurisdictionError(Exception):
    """No verified ruleset for this jurisdiction.

    Raised rather than defaulting, because a wrong-but-plausible overtime
    computation is materially worse than a loud failure.
    """


@dataclass(frozen=True)
class OvertimeRules:
    """One jurisdiction's overtime arithmetic.

    `daily_ot_after` / `daily_dt_after` are hour thresholds within one workday;
    `None` means the jurisdiction has no such rule (most states have no daily
    overtime at all and no double-time anywhere).

    `seventh_day_ot` marks jurisdictions where working all seven days of a
    workweek changes that day's classification. When set, `seventh_day_dt_after`
    is the hour count beyond which the seventh day pays double.

    `ot_multiplier` / `dt_multiplier` are the premium RATES, as distinct from the
    hour thresholds above. They live here because they are jurisdiction facts —
    the citations already state them — and because E2 gave them a second reader:
    a stored `ot`/`dot` rate may not fall below `regular x multiplier`, so the
    multiplier is now load-bearing for validation and not only for arithmetic.
    Two module-level copies (`labor.py`, `schedule_api.py`) are free to drift
    from each other and from the citation; one is not.

    `dt_multiplier` is `None` where the jurisdiction has no double time at all.
    That is the common case — most states have none — and it means "no such
    rule", never "1x".
    """

    jurisdiction: str
    citation: str  # primary source for these numbers; keep it specific
    weekly_ot_after: Decimal | None = Decimal("40")
    daily_ot_after: Decimal | None = None
    daily_dt_after: Decimal | None = None
    seventh_day_ot: bool = False
    seventh_day_dt_after: Decimal | None = None
    ot_multiplier: Decimal = Decimal("1.5")
    dt_multiplier: Decimal | None = None

    def __post_init__(self) -> None:
        if self.daily_dt_after is not None and self.daily_ot_after is None:
            raise ValueError(
                f"{self.jurisdiction}: double-time threshold without a daily "
                "overtime threshold is not a shape this engine models"
            )
        if (
            self.daily_ot_after is not None
            and self.daily_dt_after is not None
            and self.daily_dt_after <= self.daily_ot_after
        ):
            raise ValueError(
                f"{self.jurisdiction}: daily_dt_after must exceed daily_ot_after"
            )
        if self.seventh_day_dt_after is not None and not self.seventh_day_ot:
            raise ValueError(
                f"{self.jurisdiction}: seventh_day_dt_after set without seventh_day_ot"
            )
        # A jurisdiction that pays double time must say at what rate. Leaving it
        # None would make `rate_on` treat a real double-time rule as having no
        # statutory floor, so an underpaying stored rate would pass validation in
        # exactly the state that forbids it.
        pays_double = (
            self.daily_dt_after is not None or self.seventh_day_dt_after is not None
        )
        if pays_double and self.dt_multiplier is None:
            raise ValueError(
                f"{self.jurisdiction}: a double-time threshold needs a dt_multiplier"
            )


# The FLSA floor. Every state provides at least this; states without their own
# daily rule are costed under it. Kept separate from any state entry so the
# baseline is explicit rather than implied by omission.
FLSA_BASELINE = OvertimeRules(
    jurisdiction="US",
    citation="29 U.S.C. §207(a)(1); 29 CFR Part 778 — over 40 hours in a workweek at 1.5x",
    weekly_ot_after=Decimal("40"),
)


_RULES: dict[str, OvertimeRules] = {
    "US": FLSA_BASELINE,
    "US-CA": OvertimeRules(
        jurisdiction="US-CA",
        citation=(
            "Cal. Labor Code §510(a); IWC Wage Order 5 (public housekeeping) §3 — "
            "over 8 hours/day at 1.5x, over 12 hours/day at 2x, over 40 hours/week "
            "at 1.5x (no pyramiding), 7th consecutive day of a workweek: first 8 "
            "hours at 1.5x and beyond 8 at 2x"
        ),
        weekly_ot_after=Decimal("40"),
        daily_ot_after=Decimal("8"),
        daily_dt_after=Decimal("12"),
        seventh_day_ot=True,
        seventh_day_dt_after=Decimal("8"),
        ot_multiplier=Decimal("1.5"),
        dt_multiplier=Decimal("2"),
    ),
    # Additional states are added ONLY with a primary-source citation and tests.
    # Do not populate this from memory or from secondary summaries.
    #
    # Researched and deliberately NOT encoded yet — see
    # docs/reference/overtime-jurisdictions.md. Each needs a vocabulary
    # extension, and encoding a jurisdiction we do not operate in means shipping
    # a rule that goes stale unwatched (Nevada's threshold tracks the minimum
    # wage; Colorado's COMPS Order is revised roughly annually):
    #
    #   Alaska      >8/day at 1.5x, NO double time (AS 23.10.060). Cleanly
    #               expressible today except the <4-employee exemption, which is
    #               an employer attribute rather than a rule threshold. This is
    #               the natural next state to add.
    #   Colorado    greater-of {40/week, 12/workday, 12 CONSECUTIVE hours}. The
    #               consecutive-hours trigger ignores workday boundaries and has
    #               no representation here.
    #   Nevada      daily rule applies only below 1.5x the state minimum wage —
    #               conditional on the employee's rate, which this value object
    #               does not see.
    #   Kentucky    7th-day premium conditional on exceeding 40 that week, and
    #               CREDITABLE against weekly OT rather than additive.
    #   Oregon      >10/day for manufacturing/canneries only — industry-scoped.
    #   Puerto Rico multiplier depends on hire date (2x for pre-2017 hires).
    #
    # NOTE for whoever adds Alaska: several secondary sources claim Alaska has
    # double time. It does not. Both AS 23.10.060 and Alaska DOL's own summary
    # cap at 1.5x. Do not copy that claim in.
}


def rules_for(jurisdiction: str | None) -> OvertimeRules:
    if jurisdiction is None:
        raise UnknownJurisdictionError(
            "this property has no wage jurisdiction on file, so its hours cannot "
            "be costed. A property can be created by PMS ingestion, which knows "
            "nothing about wage law -- so the jurisdiction is stated by a human "
            "before payroll runs, never inferred. Set it on the property."
        )
    try:
        return _RULES[jurisdiction]
    except KeyError:
        raise UnknownJurisdictionError(
            f"no verified overtime ruleset for {jurisdiction!r}. "
            f"Known: {', '.join(sorted(_RULES))}. Refusing to fall back to another "
            "jurisdiction's rules -- that would silently miscost every hour. Add "
            "the ruleset with a primary-source citation and tests."
        ) from None


def known_jurisdictions() -> frozenset[str]:
    return frozenset(_RULES)
