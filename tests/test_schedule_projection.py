"""Pure projection-engine tests (Pillar D1) — no database."""

from datetime import date, time, timedelta
from decimal import Decimal

from usali.overtime_rules import rules_for
from usali.schedule_projection import (
    ScheduledShift,
    project_week,
    shift_hours,
    shifts_overlap,
)

_MON = date(2026, 7, 6)  # a Monday on the payroll grid (anchor 2026-01-05)


def _shift(day_offset, start, end, *, emp=1, dept=10, crosses=False):
    """start/end accept an hour int or a datetime.time."""
    return ScheduledShift(
        business_date=_MON + timedelta(days=day_offset),
        department_id=dept,
        start_time=start if isinstance(start, time) else time(start),
        end_time=end if isinstance(end, time) else time(end),
        crosses_midnight=crosses,
        employee_id=emp,
    )


def test_shift_hours_short_shift_no_meal():
    assert shift_hours(
        _shift(0, 9, 13), meal_threshold_hours=6, meal_deduction_minutes=30
    ) == Decimal("4.00")


def test_shift_hours_long_shift_deducts_meal():
    # 9h span, over the 6h threshold -> minus 30min = 8.5h
    assert shift_hours(
        _shift(0, 8, 17), meal_threshold_hours=6, meal_deduction_minutes=30
    ) == Decimal("8.50")


def test_shift_hours_exactly_at_threshold_does_not_deduct():
    # EXACTLY 6h (360 min) is not "longer than" the threshold -> no meal.
    assert shift_hours(
        _shift(0, 9, 15), meal_threshold_hours=6, meal_deduction_minutes=30
    ) == Decimal("6.00")


def test_shift_hours_one_minute_over_threshold_deducts():
    # 6h01m (361 min) crosses the threshold -> minus 30min = 331 min = 5.52h.
    assert shift_hours(
        _shift(0, 9, time(15, 1)), meal_threshold_hours=6, meal_deduction_minutes=30
    ) == Decimal("5.52")


def test_shift_hours_crosses_midnight():
    # 23:00 -> 07:00 next day = 8h span -> minus meal = 7.5h
    assert shift_hours(
        _shift(0, 23, 7, crosses=True), meal_threshold_hours=6, meal_deduction_minutes=30
    ) == Decimal("7.50")


def _project(shifts, exempt=None, context=None, actual=None):
    return project_week(
        shifts,
        week_start=_MON,
        exempt_employee_ids=exempt or set(),
        rules=rules_for("US-CA"),
        meal_threshold_hours=6,
        meal_deduction_minutes=30,
        min_rest_hours=10,
        context_shifts=context,
        actual_hours=actual,
    )


def test_five_normal_days_produce_no_warnings():
    shifts = [_shift(d, 9, 17) for d in range(5)]  # 5 x 7.5h = 37.5h
    result = _project(shifts)
    [emp] = result.employees
    assert emp.total_hours == Decimal("37.50")
    assert emp.ot_hours == Decimal("0.00")
    assert result.warnings == []
    # day_rows carry the per-day split the totals were derived from.
    assert [r.business_date for r in emp.day_rows] == [
        _MON + timedelta(days=d) for d in range(5)
    ]
    assert all(
        r.regular_hours == Decimal("7.50") and r.ot_hours == 0 and r.dt_hours == 0
        for r in emp.day_rows
    )


def test_scheduled_daily_overtime_warns_on_the_day():
    shifts = [_shift(0, 7, 17)]  # 10h span - 0.5 meal = 9.5h -> 1.5h daily OT
    result = _project(shifts)
    [w] = result.warnings
    assert w.code == "scheduled_overtime"
    assert w.employee_id == 1 and w.business_date == _MON
    assert w.hours == Decimal("1.50")
    # The day row exposes the priceable split: 8h at 1x, 1.5h at 1.5x.
    [emp] = result.employees
    [row] = emp.day_rows
    assert row.regular_hours == Decimal("8")
    assert row.ot_hours == Decimal("1.50") and row.dt_hours == 0


def test_scheduled_weekly_overtime_warns():
    # Six 8.5h-span days (8h worked after the meal) = 48h -> 8h weekly OT.
    shifts = [_shift(d, 8, time(16, 30)) for d in range(6)]
    result = _project(shifts)
    total = sum(
        (w.hours for w in result.warnings if w.code == "scheduled_overtime"), Decimal("0")
    )
    assert total == Decimal("8.00")


def test_seventh_consecutive_day_warns():
    shifts = [_shift(d, 9, 13) for d in range(7)]  # 4h/day, well under 40
    result = _project(shifts)
    assert any(
        w.code == "seventh_day" and w.business_date == _MON + timedelta(days=6)
        for w in result.warnings
    )


def test_seventh_day_warning_hours_match_priced_ot():
    # 7 x 8h worked (8:00-16:30 spans). Engine: days 1-5 fill the weekly 40, so
    # day 6's full 8h converts to weekly OT; day 7 is the 7th-day premium (its
    # first 8h at 1.5x). Total priced OT = 16h out of 56 worked; the
    # scheduled_overtime warnings must sum to exactly what the engine prices.
    shifts = [_shift(d, 8, time(16, 30)) for d in range(7)]
    result = _project(shifts)
    [emp] = result.employees
    assert emp.total_hours == Decimal("56.00")
    assert emp.ot_hours == Decimal("16.00")
    warned = sum(
        (w.hours for w in result.warnings if w.code == "scheduled_overtime"), Decimal("0")
    )
    assert warned == emp.ot_hours
    assert any(w.code == "seventh_day" for w in result.warnings)
    # day_rows reconcile with the totals — pricing the rows prices the week.
    assert sum((r.regular_hours for r in emp.day_rows), Decimal("0")) == emp.regular_hours
    assert sum((r.ot_hours + r.dt_hours for r in emp.day_rows), Decimal("0")) == emp.ot_hours


def test_clopening_warns_on_the_second_shift():
    # Close at 23:00, open at 07:00 next day -> 8h rest < 10h floor.
    shifts = [_shift(0, 15, 23), _shift(1, 7, 15)]
    result = _project(shifts)
    [w] = [w for w in result.warnings if w.code == "clopening"]
    assert w.business_date == _MON + timedelta(days=1)


def test_clopening_across_a_midnight_shift():
    # Audit 23:00->07:00 (ends Tue 07:00), next shift Tue 15:00 -> 8h rest -> warns.
    shifts = [_shift(0, 23, 7, crosses=True), _shift(1, 15, 23)]
    result = _project(shifts)
    assert any(w.code == "clopening" for w in result.warnings)


def test_same_day_split_shift_short_gap_is_not_clopening():
    # A split shift (breakfast + dinner service) with a 3h afternoon gap is
    # normal hotel practice — the rest floor is about overnight rest, so a
    # same-day gap that never spans a night must NOT warn.
    shifts = [_shift(0, 7, 11), _shift(0, 14, 19)]
    result = _project(shifts)
    assert not any(w.code == "clopening" for w in result.warnings)
    # And the day's hours SUM across the split: 4h + 5h = 9h -> 1h daily OT.
    [emp] = result.employees
    assert emp.total_hours == Decimal("9.00")
    [w] = [w for w in result.warnings if w.code == "scheduled_overtime"]
    assert w.hours == Decimal("1.00") and w.business_date == _MON


def test_overlapping_shifts_do_not_crash_and_do_not_warn_clopening():
    # The API 422s overlap before projection ever runs, but the engine must
    # tolerate it: negative rest is skipped, hours still sum.
    shifts = [_shift(0, 9, 17), _shift(0, 16, 22)]
    result = _project(shifts)
    assert not any(w.code == "clopening" for w in result.warnings)
    [emp] = result.employees
    assert emp.total_hours == Decimal("13.50")  # 7.5 + 6.0


def test_exempt_employee_projects_hours_but_never_ot():
    shifts = [_shift(d, 7, 19) for d in range(6)]  # long days
    result = _project(shifts, exempt={1})
    [emp] = result.employees
    assert emp.ot_hours == Decimal("0.00") and emp.total_hours > Decimal("60")
    assert not any(w.code == "scheduled_overtime" for w in result.warnings)
    # Exempt day rows are all-regular: nothing in them is OT-priceable.
    assert all(r.ot_hours == 0 and r.dt_hours == 0 for r in emp.day_rows)


def test_open_shifts_count_dept_hours_but_no_employee_or_warning():
    shifts = [_shift(0, 9, 17, emp=None)]
    result = _project(shifts)
    assert result.employees == []
    assert result.warnings == []
    assert result.department_hours[10] == Decimal("7.50")


def test_overlap_detection_helper():
    a, b = _shift(0, 9, 17), _shift(0, 16, 22)
    assert shifts_overlap(a, b)
    c = _shift(0, 17, 22)
    assert not shifts_overlap(a, c)  # back-to-back is not overlap
    # Midnight-crossing: Mon 23->07 overlaps a Tue 06->14 shift.
    d, e = _shift(0, 23, 7, crosses=True), _shift(1, 6, 14)
    assert shifts_overlap(d, e)


# --- D3: cross-week clopening context ---------------------------------------


def test_context_prior_week_close_warns_clopening_on_in_week_open():
    # Prior-week Sunday 15:00-23:00 (context) then THIS week's Monday 07:00
    # open -> 8h rest < 10h floor, spans a night, second shift is in-week
    # -> the clopening warning lands ON the Monday shift.
    context = [_shift(-1, 15, 23)]
    shifts = [_shift(0, 7, 15)]
    result = _project(shifts, context=context)
    [w] = [w for w in result.warnings if w.code == "clopening"]
    assert w.employee_id == 1 and w.business_date == _MON
    # Context contributes NOTHING to hours/OT/department_hours.
    [emp] = result.employees
    assert emp.total_hours == Decimal("7.50")
    assert emp.ot_hours == Decimal("0.00")
    assert result.department_hours == {10: Decimal("7.50")}


def test_context_second_shift_of_pair_attributes_to_the_other_week():
    # THIS week's Sunday close + NEXT week's Monday open (context): the
    # sub-floor pair exists, but its SECOND shift is out-of-week — the
    # mirror-image warning belongs to the other week's projection, not this one.
    shifts = [_shift(6, 15, 23)]  # this week's Sunday
    context = [_shift(7, 7, 15)]  # next week's Monday
    result = _project(shifts, context=context)
    assert not any(w.code == "clopening" for w in result.warnings)


def test_context_midnight_crossing_close_pairs_by_span_order():
    # Context Sunday-night audit 23:00->07:00 ends MONDAY 07:00; the in-week
    # Monday 15:00 shift starts 8h later. Sorting is by span start (the
    # context shift sorts first even though its span reaches into Monday).
    context = [_shift(-1, 23, 7, crosses=True)]
    shifts = [_shift(0, 15, 23)]
    result = _project(shifts, context=context)
    [w] = [w for w in result.warnings if w.code == "clopening"]
    assert w.business_date == _MON


def test_context_only_employee_produces_no_row():
    shifts = [_shift(0, 9, 17, emp=1)]
    context = [_shift(-1, 9, 17, emp=2, dept=20), _shift(-1, 9, 17, emp=None, dept=20)]
    result = _project(shifts, context=context)
    assert [e.employee_id for e in result.employees] == [1]
    assert result.warnings == []
    assert result.department_hours == {10: Decimal("7.50")}


# --- D3: actual-hours merge --------------------------------------------------


def test_actual_hours_replace_not_add_on_a_scheduled_day():
    # Five 8h scheduled days (8:00-16:30, meal out) = 40h flat. Monday actually
    # punched 9.50 -> the merged week is 9.5 + 4x8 = 41.50 (NOT 8+9.5 on Monday).
    shifts = [_shift(d, 8, time(16, 30)) for d in range(5)]
    result = _project(shifts, actual={1: {_MON: Decimal("9.50")}})
    [emp] = result.employees
    assert emp.total_hours == Decimal("41.50")
    # Daily OT on the merged Monday: 1.5h over the 8h daily line.
    assert emp.ot_hours == Decimal("1.50")
    [w] = [w for w in result.warnings if w.code == "scheduled_overtime"]
    assert w.business_date == _MON and w.hours == Decimal("1.50")


def test_actual_hours_day_with_no_shift_counts_toward_weekly_ot():
    # 40 scheduled hours Mon-Fri; Saturday has NO shift but 6.00 punched hours
    # (worked unscheduled) -> weekly OT math must see 46h -> 6h weekly OT,
    # attributed to Saturday by the merged math.
    shifts = [_shift(d, 8, time(16, 30)) for d in range(5)]
    sat = _MON + timedelta(days=5)
    result = _project(shifts, actual={1: {sat: Decimal("6.00")}})
    [emp] = result.employees
    assert emp.total_hours == Decimal("46.00")
    assert emp.ot_hours == Decimal("6.00")
    [w] = [w for w in result.warnings if w.code == "scheduled_overtime"]
    assert w.business_date == sat and w.hours == Decimal("6.00")
    # The merged Saturday appears in the priceable day rows.
    assert sat in {r.business_date for r in emp.day_rows}


def test_actual_hours_only_day_does_not_trigger_seventh_day_warning():
    # Six scheduled days + a punched-only Sunday: the SEVENTH-DAY warning is
    # about the PLAN (shifts only) and must not fire. The OT math, however,
    # honestly prices the really-worked 7 days (compute_overtime's 7th-day
    # premium applies to the merged hours).
    shifts = [_shift(d, 9, 13) for d in range(6)]  # 6 x 4h = 24h
    sun = _MON + timedelta(days=6)
    result = _project(shifts, actual={1: {sun: Decimal("4.00")}})
    assert not any(w.code == "seventh_day" for w in result.warnings)
    [emp] = result.employees
    assert emp.total_hours == Decimal("28.00")
    assert emp.ot_hours == Decimal("4.00")  # 7th-day premium on the worked Sunday


def test_actual_hours_do_not_disturb_shift_derived_clopening():
    # Clopening pairs come from SHIFTS: a zero-hour actual override on the
    # second day does not un-pair the scheduled close/open.
    shifts = [_shift(0, 15, 23), _shift(1, 7, 15)]
    tue = _MON + timedelta(days=1)
    result = _project(shifts, actual={1: {tue: Decimal("0.00")}})
    [w] = [w for w in result.warnings if w.code == "clopening"]
    assert w.business_date == tue
    [emp] = result.employees
    assert emp.total_hours == Decimal("7.50")  # Monday only; Tuesday punched 0


def test_actual_hours_for_other_employees_do_not_leak():
    shifts = [_shift(0, 9, 17, emp=1)]
    result = _project(shifts, actual={2: {_MON: Decimal("8.00")}})
    emp1 = next(e for e in result.employees if e.employee_id == 1)
    assert emp1.total_hours == Decimal("7.50")
    # Employee 2 worked unscheduled all on their own — the merged projection
    # still surfaces those hours as an employee row (honest weekly picture).
    emp2 = next(e for e in result.employees if e.employee_id == 2)
    assert emp2.total_hours == Decimal("8.00")
    # ...but department_hours stay schedule-derived.
    assert result.department_hours == {10: Decimal("7.50")}
