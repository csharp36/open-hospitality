from datetime import date, timedelta

import pytest

from usali.fiscal import (
    FiscalCalendarNotConfigured,
    FiscalConfig,
    _fy_anchor,
    period_containing,
    periods_in_year,
    resolve_period,
)


# --- calendar month ---------------------------------------------------------

def test_calendar_month_january_start():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=1,
                       week_start_weekday=None)
    assert resolve_period(cfg, "2026-P01") == (date(2026, 1, 1), date(2026, 1, 31))
    assert resolve_period(cfg, "2026-P02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert resolve_period(cfg, "2026-P12") == (date(2026, 12, 1), date(2026, 12, 31))


def test_calendar_month_july_start_wraps_year():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=7,
                       week_start_weekday=None)
    assert resolve_period(cfg, "2026-P01") == (date(2026, 7, 1), date(2026, 7, 31))
    assert resolve_period(cfg, "2026-P07") == (date(2027, 1, 1), date(2027, 1, 31))


def test_calendar_month_leap_february():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=1,
                       week_start_weekday=None)
    assert resolve_period(cfg, "2024-P02") == (date(2024, 2, 1), date(2024, 2, 29))


def test_period_containing_calendar_month():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=7,
                       week_start_weekday=None)
    assert period_containing(cfg, date(2027, 1, 15)) == "2026-P07"
    assert period_containing(cfg, date(2026, 7, 1)) == "2026-P01"


# --- 4-4-5 ------------------------------------------------------------------

def test_445_periods_tile_the_year_without_gaps():
    # FY start month January, weeks start Sunday (weekday 6).
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    periods = periods_in_year(cfg, 2026)
    assert len(periods) == 12
    # first period starts on the first Sunday on/after 2026-01-01 (2026-01-04)
    assert periods[0][1] == date(2026, 1, 4)
    # 4-week, 4-week, 5-week quarter shape
    assert (periods[0][2] - periods[0][1]).days + 1 == 28   # P1 = 4 weeks
    assert (periods[2][2] - periods[2][1]).days + 1 == 35   # P3 = 5 weeks
    # contiguous, no gaps
    for (_, _, prev_end), (_, nxt_start, _) in zip(periods, periods[1:]):
        assert nxt_start == prev_end + __import__("datetime").timedelta(days=1)


def test_445_anchor_is_start_month_first_when_it_lands_on_the_weekday():
    # 2023-01-01 is a Sunday -> anchor is that day exactly ("on or after").
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    assert resolve_period(cfg, "2023-P01")[0] == date(2023, 1, 1)


def test_445_final_period_absorbs_a_53rd_week():
    """A genuinely 53-week fiscal year: the year's last period runs 6 weeks
    so the calendar tiles right up to the next year's anchor.

    FY2012 is 53 weeks under this anchor scheme: anchor(2012) = 2012-01-01
    (a Sunday, so it IS the anchor), anchor(2013) = 2013-01-06, and
    (2013-01-06 - 2012-01-01).days == 371 == 53 * 7. Verified directly
    against `_fy_anchor` rather than assumed."""
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    anchor_2012 = _fy_anchor(cfg, 2012)
    anchor_2013 = _fy_anchor(cfg, 2013)
    assert anchor_2012 == date(2012, 1, 1)
    assert anchor_2013 == date(2013, 1, 6)
    assert (anchor_2013 - anchor_2012).days == 53 * 7  # confirms FY2012 is 53 weeks

    periods = periods_in_year(cfg, 2012)
    final_key, final_start, final_end = periods[-1]
    assert final_key == "2012-P12"
    assert final_end == anchor_2013 - timedelta(days=1)
    assert (final_end - final_start).days + 1 == 6 * 7  # strictly 6 weeks, the absorption branch
    assert period_containing(cfg, final_end) == "2012-P12"  # round-trips back to P12


def test_445_final_period_is_five_weeks_in_a_normal_52_week_year():
    """Pin the non-absorbing branch too: FY2020 is a plain 52-week year
    (anchor 2020-01-05 -> anchor 2021-01-03, 364 days), so the final period
    should be exactly 5 weeks, not 6."""
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    anchor_2020 = _fy_anchor(cfg, 2020)
    anchor_2021 = _fy_anchor(cfg, 2021)
    assert (anchor_2021 - anchor_2020).days == 52 * 7  # confirms FY2020 is 52 weeks

    periods = periods_in_year(cfg, 2020)
    final_key, final_start, final_end = periods[-1]
    assert final_key == "2020-P12"
    assert final_end == anchor_2021 - timedelta(days=1)
    assert (final_end - final_start).days + 1 == 5 * 7  # strictly 5 weeks, no absorption


def test_period_containing_445_round_trips():
    cfg = FiscalConfig(calendar_type="445", fiscal_year_start_month=1, week_start_weekday=6)
    for key, start, end in periods_in_year(cfg, 2026):
        assert period_containing(cfg, start) == key
        assert period_containing(cfg, end) == key


def test_resolve_rejects_bad_period_number():
    cfg = FiscalConfig(calendar_type="calendar_month", fiscal_year_start_month=1,
                       week_start_weekday=None)
    with pytest.raises(ValueError):
        resolve_period(cfg, "2026-P13")
    with pytest.raises(ValueError):
        resolve_period(cfg, "2026-P00")


def test_not_configured_raises():
    with pytest.raises(FiscalCalendarNotConfigured):
        # Loading helper (Task 5 uses it too); None config => refuse.
        from usali.fiscal import require_config
        require_config(None)
