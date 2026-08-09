"""The jurisdiction seam, and above all what it REFUSES.

The engine is reached by every costing path. Before this seam existed,
California's thresholds were module constants, so a property in another state
would have been costed under California daily overtime silently -- the
arithmetic succeeds, the numbers are just wrong.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from usali.overtime import compute_overtime
from usali.overtime_rules import (
    FLSA_BASELINE,
    OvertimeRules,
    UnknownJurisdictionError,
    known_jurisdictions,
    rules_for,
)

_ANCHOR = date(2026, 1, 5)  # a Monday


def _days(*hours):
    return {
        _ANCHOR + timedelta(days=i): Decimal(str(h)) for i, h in enumerate(hours)
    }


def _totals(day_hours, rules):
    rows = compute_overtime(day_hours, anchor=_ANCHOR, exempt=False, rules=rules)
    return (
        sum((r.regular_hours for r in rows), Decimal("0")),
        sum((r.ot_hours for r in rows), Decimal("0")),
        sum((r.dt_hours for r in rows), Decimal("0")),
    )


# --- the refusal ------------------------------------------------------------

def test_unknown_jurisdiction_is_refused_not_defaulted():
    """A Texas property must NOT quietly inherit California's daily overtime.
    Failing loudly at the first costing attempt is far cheaper than a quarter of
    plausible-looking wrong numbers."""
    with pytest.raises(UnknownJurisdictionError, match="US-TX"):
        rules_for("US-TX")


def test_refusal_message_names_the_known_set_and_forbids_fallback():
    with pytest.raises(UnknownJurisdictionError) as exc:
        rules_for("US-NV")
    assert "Refusing to fall back" in str(exc.value)
    assert "US-CA" in str(exc.value)


def test_only_verified_jurisdictions_are_registered():
    """States are added ONLY with a primary-source citation and tests. If this
    fails because someone added a state, confirm they cited a statute."""
    assert known_jurisdictions() == {"US", "US-CA"}


def test_every_registered_ruleset_carries_a_citation():
    for jurisdiction in known_jurisdictions():
        rules = rules_for(jurisdiction)
        assert rules.citation.strip(), f"{jurisdiction} has no citation"
        assert any(ch.isdigit() for ch in rules.citation), (
            f"{jurisdiction} citation cites no section number: {rules.citation!r}"
        )


# --- shape validation -------------------------------------------------------

def test_double_time_without_daily_overtime_is_rejected():
    with pytest.raises(ValueError, match="not a shape this engine models"):
        OvertimeRules(
            jurisdiction="X", citation="§1", daily_dt_after=Decimal("12")
        )


def test_double_time_threshold_must_exceed_overtime_threshold():
    with pytest.raises(ValueError, match="must exceed"):
        OvertimeRules(
            jurisdiction="X", citation="§1",
            daily_ot_after=Decimal("12"), daily_dt_after=Decimal("8"),
        )


def test_seventh_day_doubletime_requires_the_seventh_day_rule():
    with pytest.raises(ValueError, match="without seventh_day_ot"):
        OvertimeRules(
            jurisdiction="X", citation="§1", seventh_day_dt_after=Decimal("8")
        )


# --- the FLSA floor behaves as a weekly-only jurisdiction --------------------

def test_flsa_baseline_has_no_daily_overtime():
    """Five 10-hour days is 50 hours: 40 regular + 10 weekly OT, and NO daily
    overtime. Under California the same week yields 10 hours of DAILY OT
    instead -- same total premium here by coincidence, so the discriminating
    case is the next test."""
    reg, ot, dt = _totals(_days(10, 10, 10, 10, 10), FLSA_BASELINE)
    assert (reg, ot, dt) == (Decimal("40"), Decimal("10"), Decimal("0"))


def test_flsa_and_california_diverge_on_a_short_week_with_one_long_day():
    """THE case that proves the seam matters. One 13-hour day and nothing else
    is 13 hours: under the FLSA floor it is all straight time (under 40 for the
    week). Under California it is 8 regular + 4 OT + 1 double-time.

    Costing a California property under the federal floor would understate that
    day's labor; costing a federal-only state under California rules would
    overstate it. Both are silent."""
    flsa = _totals(_days(13), FLSA_BASELINE)
    california = _totals(_days(13), rules_for("US-CA"))

    assert flsa == (Decimal("13"), Decimal("0"), Decimal("0"))
    assert california == (Decimal("8"), Decimal("4"), Decimal("1"))
    assert flsa != california


def test_flsa_baseline_has_no_seventh_day_premium():
    """Seven 8-hour days under the federal floor is 56 hours: 40 regular and 16
    weekly OT, with no seventh-day rule. California would classify the seventh
    day's 8 hours as OT via its own premium instead."""
    reg, ot, dt = _totals(_days(8, 8, 8, 8, 8, 8, 8), FLSA_BASELINE)
    assert (reg, ot, dt) == (Decimal("40"), Decimal("16"), Decimal("0"))
