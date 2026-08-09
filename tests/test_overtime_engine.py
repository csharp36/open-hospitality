from datetime import date, timedelta
from decimal import Decimal

from usali.overtime import compute_overtime
from usali.overtime_rules import rules_for

_CA = rules_for("US-CA")

_ANCHOR = date(2026, 1, 5)  # a Monday — the workweek start


def _mon(week_offset=0):
    return _ANCHOR + timedelta(days=7 * week_offset)


def _hours(pairs):
    return {d: Decimal(str(h)) for d, h in pairs}


def _one(day_hours, *, exempt=False, rules=_CA):
    return {
        r.business_date: r
        for r in compute_overtime(day_hours, anchor=_ANCHOR, exempt=exempt, rules=rules)
    }


def test_eight_hour_day_is_all_regular():
    r = _one(_hours([(_mon(), 8)]))[_mon()]
    assert (r.regular_hours, r.ot_hours, r.dt_hours) == (Decimal("8"), Decimal("0"), Decimal("0"))


def test_daily_overtime_over_eight():
    r = _one(_hours([(_mon(), 10)]))[_mon()]
    assert (r.regular_hours, r.ot_hours, r.dt_hours) == (Decimal("8"), Decimal("2"), Decimal("0"))


def test_daily_doubletime_over_twelve():
    r = _one(_hours([(_mon(), 13)]))[_mon()]
    assert (r.regular_hours, r.ot_hours, r.dt_hours) == (Decimal("8"), Decimal("4"), Decimal("1"))


def test_weekly_overtime_over_forty_with_no_daily_ot():
    # Six 8h days = 48h. No day exceeds 8, so daily OT is zero; the 8 hours over
    # 40 in the week are weekly OT, landing on the day that crossed the line.
    days = [(_mon() + timedelta(days=i), 8) for i in range(6)]
    res = _one(_hours(days))
    reg = sum(r.regular_hours for r in res.values())
    ot = sum(r.ot_hours for r in res.values())
    assert (reg, ot) == (Decimal("40"), Decimal("8"))


def test_no_pyramiding_daily_ot_not_recounted_weekly():
    # Five 9h days = 45h. Each day is 8 reg + 1 daily-OT → 40 reg, 5 OT.
    # Weekly-40 must NOT also convert regular hours (reg is exactly 40).
    days = [(_mon() + timedelta(days=i), 9) for i in range(5)]
    res = _one(_hours(days))
    assert sum(r.regular_hours for r in res.values()) == Decimal("40")
    assert sum(r.ot_hours for r in res.values()) == Decimal("5")


def test_seventh_consecutive_day_is_premium():
    # 7×8h. Days 1–5 = 40 reg; day 6 (over 40) = 8 weekly-OT; day 7 (7th
    # consecutive) = 8 OT at 1.5×. Total 40 reg, 16 OT, 0 DT.
    days = [(_mon() + timedelta(days=i), 8) for i in range(7)]
    res = _one(_hours(days))
    assert sum(r.regular_hours for r in res.values()) == Decimal("40")
    assert sum(r.ot_hours for r in res.values()) == Decimal("16")
    assert sum(r.dt_hours for r in res.values()) == Decimal("0")


def test_seventh_day_over_eight_is_doubletime():
    # 6×8h then a 10h seventh day. Seventh day: first 8h @1.5×, 2h @2×.
    days = [(_mon() + timedelta(days=i), 8) for i in range(6)] + [(_mon() + timedelta(days=6), 10)]
    res = _one(_hours(days))
    assert res[_mon() + timedelta(days=6)].dt_hours == Decimal("2")


def test_exempt_employee_has_no_overtime():
    days = [(_mon() + timedelta(days=i), 12) for i in range(6)]
    res = _one(_hours(days), exempt=True)
    assert sum(r.ot_hours for r in res.values()) == Decimal("0")
    assert sum(r.dt_hours for r in res.values()) == Decimal("0")
    assert sum(r.regular_hours for r in res.values()) == Decimal("72")


def test_two_workweeks_are_independent():
    # 40h in week 1, 40h in week 2 → no weekly OT in either.
    days = [(_mon(0) + timedelta(days=i), 8) for i in range(5)] + \
           [(_mon(1) + timedelta(days=i), 8) for i in range(5)]
    res = _one(_hours(days))
    assert sum(r.ot_hours for r in res.values()) == Decimal("0")
    assert sum(r.regular_hours for r in res.values()) == Decimal("80")


# --- Additional adversarial cases (not in the plan; strengthen the core) ---


def test_seventh_day_premium_applies_even_under_forty():
    # 7 days of 4h each = 28h, well under 40. The 7th consecutive day is STILL
    # premium: CA's 7th-day rule is independent of the weekly-40 total. Days 1–6
    # are 24 regular hours; day 7's 4 hours are all OT (1.5×), none regular.
    days = [(_mon() + timedelta(days=i), 4) for i in range(7)]
    res = _one(_hours(days))
    assert sum(r.regular_hours for r in res.values()) == Decimal("24")
    assert sum(r.ot_hours for r in res.values()) == Decimal("4")
    assert sum(r.dt_hours for r in res.values()) == Decimal("0")
    # The premium is on the 7th day specifically.
    assert res[_mon() + timedelta(days=6)].regular_hours == Decimal("0")
    assert res[_mon() + timedelta(days=6)].ot_hours == Decimal("4")


def test_daily_doubletime_without_weekly_overflow_no_pyramiding():
    # Three 14h days = 42h. Daily: each 8 reg + 4 OT + 2 DT. Regular hours total
    # only 24 (< 40), so weekly-40 adds nothing — hours already paid as daily
    # OT/DT are not recounted toward the 40.
    days = [(_mon() + timedelta(days=i), 14) for i in range(3)]
    res = _one(_hours(days))
    assert sum(r.regular_hours for r in res.values()) == Decimal("24")
    assert sum(r.ot_hours for r in res.values()) == Decimal("12")
    assert sum(r.dt_hours for r in res.values()) == Decimal("6")


def test_fractional_hours_classify_exactly_no_float():
    # 8.5h → 8 reg + 0.5 OT, exactly (no binary-float drift).
    r = _one(_hours([(_mon(), "8.5")]))[_mon()]
    assert r.regular_hours == Decimal("8")
    assert r.ot_hours == Decimal("0.5")
    assert r.dt_hours == Decimal("0")


def test_partial_week_of_six_days_gets_no_seventh_day_premium():
    # Worked 6 of 7 days (a gap on day 3). No day exceeds 8. 48 worked hours →
    # 40 reg + 8 weekly OT, and crucially NO 7th-day premium (only 6 days worked).
    offsets = [0, 1, 3, 4, 5, 6]  # day index 2 (Wed) skipped
    days = [(_mon() + timedelta(days=i), 8) for i in offsets]
    res = _one(_hours(days))
    assert sum(r.regular_hours for r in res.values()) == Decimal("40")
    assert sum(r.ot_hours for r in res.values()) == Decimal("8")
    assert sum(r.dt_hours for r in res.values()) == Decimal("0")
