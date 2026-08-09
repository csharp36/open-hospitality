"""E4 Task 1: the California sick-leave ruleset, pinned to the statute.

Every number here traces to docs/reference/ca-sick-leave.md (researched
2026-07-20 against Labor Code §246 as amended by SB 616 and the DIR FAQ).
The DIR's own worked examples are pinned as tests — if the encoding drifts
from the agency's reading, a test names the discrepancy.
"""

from datetime import date
from decimal import Decimal

import pytest

from usali.sick_leave_rules import (
    UnknownSickLeaveJurisdiction,
    sick_leave_rules_for,
)


def test_california_accrues_one_hour_per_thirty_worked():
    rules = sick_leave_rules_for("US-CA")
    assert rules.hours_worked_per_accrued_hour == Decimal("30")


def test_the_caps_honour_whichever_is_more():
    """The DIR's own examples. 'Five days or 40 hours, whichever is more':
    a 10-hour/day employee's usage floor is 50 hours (5 x 10) and their
    accrual cap 100 (10 x 10); a 6-hour/day employee keeps the statutory
    40/80 floors — days-based figures never REDUCE the hours-based ones."""
    rules = sick_leave_rules_for("US-CA")
    assert rules.usage_cap_hours(day_length=Decimal("10")) == Decimal("50")
    assert rules.accrual_cap_hours(day_length=Decimal("10")) == Decimal("100")
    assert rules.usage_cap_hours(day_length=Decimal("6")) == Decimal("40")
    assert rules.accrual_cap_hours(day_length=Decimal("6")) == Decimal("80")
    assert rules.usage_cap_hours(day_length=Decimal("8")) == Decimal("40")
    assert rules.accrual_cap_hours(day_length=Decimal("8")) == Decimal("80")


def test_exempt_staff_are_deemed_a_forty_hour_week():
    rules = sick_leave_rules_for("US-CA")
    assert rules.exempt_deemed_weekly_hours == Decimal("40")


def test_the_flsa_floor_is_an_encoded_absence():
    """Plain `US` returns None: federal law mandates no paid sick leave, so
    zero accrual there is encoded law — 'no entries appeared' must never be
    ambiguous between correct and broken."""
    assert sick_leave_rules_for("US") is None


def test_an_unknown_jurisdiction_is_refused_not_zeroed():
    """Unknown STATES refuse loudly: most (NV, CO, …) have leave statutes
    this module has not encoded, and accruing zero for them would be
    silently unlawful — the rules_for() posture from overtime_rules."""
    for juris in ("US-NV", "", "CA"):
        with pytest.raises(UnknownSickLeaveJurisdiction):
            sick_leave_rules_for(juris)


def test_the_ruleset_carries_its_citation_and_recheck_date():
    """Encoded law goes stale: the statute has been amended repeatedly and
    the reference doc sets a re-check date. The ruleset carries both so a
    consumer can warn when the date passes, naming the doc to re-verify."""
    rules = sick_leave_rules_for("US-CA")
    assert "246" in rules.citation
    assert rules.recheck_by == date(2027, 1, 1)
    assert "ca-sick-leave" in rules.reference_doc
