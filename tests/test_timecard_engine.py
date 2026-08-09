from datetime import UTC, date, datetime

from usali.timecards import Event, compute_day, period_for

_ANCHOR = date(2026, 1, 5)  # a Monday


def _at(h, m=0):
    return datetime(2026, 7, 7, h, m, tzinfo=UTC)


def _ev(t, h, m=0):
    return Event(punch_type=t, at=_at(h, m))


def test_simple_shift_hours():
    day = compute_day(date(2026, 7, 7), [_ev("clock_in", 9), _ev("clock_out", 17)])
    assert day.worked_minutes == 480  # 8h
    # An 8h shift with no lunch punched IS a CA meal violation — the hours are
    # clean, the compliance is not. (The plan's own Task 5 detail test asserts
    # the same `no_meal_break` for this exact shift.)
    assert day.warnings == ["no_meal_break"]


def test_lunch_is_unpaid_and_subtracted():
    day = compute_day(date(2026, 7, 7), [
        _ev("clock_in", 9), _ev("lunch_start", 12), _ev("lunch_end", 12, 30),
        _ev("clock_out", 17),
    ])
    assert day.worked_minutes == 450  # 8h shift minus a 30m unpaid lunch
    assert day.warnings == []


def test_missing_clock_out_is_flagged_and_pays_nothing_for_that_span():
    day = compute_day(date(2026, 7, 7), [_ev("clock_in", 9)])
    assert "missing_clock_out" in day.warnings
    assert day.worked_minutes == 0  # an unclosed span is never silently paid


def test_missing_clock_in_is_flagged():
    day = compute_day(date(2026, 7, 7), [_ev("clock_out", 17)])
    assert "missing_clock_in" in day.warnings


def test_no_meal_break_over_five_hours_is_flagged():
    # CA: a duty-free 30-min meal is owed before the end of the 5th hour.
    day = compute_day(date(2026, 7, 7), [_ev("clock_in", 9), _ev("clock_out", 16)])
    assert "no_meal_break" in day.warnings


def test_short_meal_break_is_flagged():
    day = compute_day(date(2026, 7, 7), [
        _ev("clock_in", 9), _ev("lunch_start", 12), _ev("lunch_end", 12, 20),
        _ev("clock_out", 17),
    ])
    assert "short_meal_break" in day.warnings


def test_late_meal_break_is_flagged():
    # Meal started after the 5th hour of work.
    day = compute_day(date(2026, 7, 7), [
        _ev("clock_in", 8), _ev("lunch_start", 14), _ev("lunch_end", 14, 30),
        _ev("clock_out", 18),
    ])
    assert "late_meal_break" in day.warnings


def test_missing_lunch_end_is_flagged():
    day = compute_day(date(2026, 7, 7), [
        _ev("clock_in", 9), _ev("lunch_start", 12), _ev("clock_out", 17),
    ])
    assert "missing_lunch_end" in day.warnings


def test_short_shift_needs_no_meal_break():
    day = compute_day(date(2026, 7, 7), [_ev("clock_in", 9), _ev("clock_out", 13)])
    assert day.warnings == []
    assert day.worked_minutes == 240


def test_orphaned_clock_in_is_flagged_not_silently_dropped():
    """A second clock_in while one is open means the first was never closed.

    The 9:00 span must not vanish without a word — that is the same silent
    hours corruption the kiosk double-tap guard exists to prevent.
    """
    day = compute_day(date(2026, 7, 7), [
        _ev("clock_in", 9), _ev("clock_in", 10), _ev("clock_out", 17),
    ])
    assert "missing_clock_out" in day.warnings
    assert day.worked_minutes == 420  # only the closed 10->17 span is paid


def test_orphaned_lunch_start_is_flagged():
    """An overwritten open lunch_start is a missing lunch_end, not a free restart."""
    day = compute_day(date(2026, 7, 7), [
        _ev("clock_in", 9), _ev("lunch_start", 12), _ev("lunch_start", 13),
        _ev("lunch_end", 13, 30), _ev("clock_out", 17),
    ])
    assert "missing_lunch_end" in day.warnings


def test_period_for_is_deterministic_biweekly():
    assert period_for(date(2026, 1, 5), _ANCHOR) == (date(2026, 1, 5), date(2026, 1, 18))
    assert period_for(date(2026, 1, 18), _ANCHOR) == (date(2026, 1, 5), date(2026, 1, 18))
    assert period_for(date(2026, 1, 19), _ANCHOR) == (date(2026, 1, 19), date(2026, 2, 1))
