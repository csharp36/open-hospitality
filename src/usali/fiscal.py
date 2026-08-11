"""Fiscal-calendar period resolution (issue #8).

Pure functions over a property's fiscal-calendar config. Two calendar types:

* `calendar_month` — period N is the Nth calendar month counting from
  `fiscal_year_start_month`.
* `445` — 4/4/5-week periods. The fiscal year's anchor is the first
  `week_start_weekday` ON OR AFTER the 1st of the start month. Periods are
  consecutive week blocks (4,4,5 per quarter). A 53-week year (the next
  anchor lands 53 weeks out) is handled by the FINAL period absorbing the
  extra week — the year always tiles right up to the next anchor.

Period key format: "{fiscal_year}-P{NN}", fiscal_year = the calendar year the
fiscal year STARTS in, NN = 01..12. Both types have 12 periods.

`period_containing` and `periods_in_year` are defined in terms of
`resolve_period`, so period boundaries have a single source of truth.
"""

from dataclasses import dataclass
from datetime import date, timedelta

# period -> number of weeks, for a 4-4-5 quarter repeated four times.
_445_WEEKS = (4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 4, 5)


class FiscalCalendarNotConfigured(Exception):
    """The property has no fiscal-calendar row; periods cannot be resolved."""


@dataclass(frozen=True)
class FiscalConfig:
    calendar_type: str
    fiscal_year_start_month: int
    week_start_weekday: int | None


def require_config(config: "FiscalConfig | None") -> FiscalConfig:
    """The one place callers turn a possibly-absent row into a value or a loud
    refusal (adr-010)."""
    if config is None:
        raise FiscalCalendarNotConfigured(
            "this property has no fiscal calendar on file, so fiscal periods "
            "cannot be resolved. Configure calendar type and fiscal-year start "
            "on the property first."
        )
    return config


def _parse_key(period_key: str) -> tuple[int, int]:
    try:
        year_str, period_str = period_key.split("-P")
        fiscal_year, period = int(year_str), int(period_str)
    except (ValueError, AttributeError):
        raise ValueError(f"malformed period key {period_key!r}; expected 'YYYY-Pnn'") from None
    if not 1 <= period <= 12:
        raise ValueError(f"period number out of range in {period_key!r} (1..12)")
    return fiscal_year, period


def _add_months(d: date, months: int) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def _month_period(config: FiscalConfig, fiscal_year: int, period: int) -> tuple[date, date]:
    start = _add_months(date(fiscal_year, config.fiscal_year_start_month, 1), period - 1)
    next_start = _add_months(start, 1)
    return start, next_start - timedelta(days=1)


def _fy_anchor(config: FiscalConfig, fiscal_year: int) -> date:
    """First `week_start_weekday` on or after the 1st of the start month."""
    assert config.week_start_weekday is not None  # guaranteed for 445 by the DB pair-check
    first = date(fiscal_year, config.fiscal_year_start_month, 1)
    delta = (config.week_start_weekday - first.weekday()) % 7
    return first + timedelta(days=delta)


def _445_bounds(config: FiscalConfig, fiscal_year: int) -> list[tuple[date, date]]:
    anchor = _fy_anchor(config, fiscal_year)
    next_anchor = _fy_anchor(config, fiscal_year + 1)
    bounds: list[tuple[date, date]] = []
    cursor = anchor
    for weeks in _445_WEEKS:
        end = cursor + timedelta(weeks=weeks) - timedelta(days=1)
        bounds.append((cursor, end))
        cursor = end + timedelta(days=1)
    # Final period absorbs any 53rd week: extend it to the day before next anchor.
    last_start, _ = bounds[-1]
    bounds[-1] = (last_start, next_anchor - timedelta(days=1))
    return bounds


def resolve_period(config: FiscalConfig, period_key: str) -> tuple[date, date]:
    fiscal_year, period = _parse_key(period_key)
    if config.calendar_type == "calendar_month":
        return _month_period(config, fiscal_year, period)
    return _445_bounds(config, fiscal_year)[period - 1]


def periods_in_year(config: FiscalConfig, fiscal_year: int) -> list[tuple[str, date, date]]:
    out: list[tuple[str, date, date]] = []
    for period in range(1, 13):
        key = f"{fiscal_year}-P{period:02d}"
        start, end = resolve_period(config, key)
        out.append((key, start, end))
    return out


def period_containing(config: FiscalConfig, day: date) -> str:
    """The period key whose date range contains `day`."""
    if config.calendar_type == "calendar_month":
        # Fiscal year = the year whose start month <= day within a 12-month run.
        fiscal_year = day.year if day.month >= config.fiscal_year_start_month else day.year - 1
    else:
        # Largest fiscal_year whose anchor <= day.
        fiscal_year = day.year + 1
        while _fy_anchor(config, fiscal_year) > day:
            fiscal_year -= 1
    for key, start, end in periods_in_year(config, fiscal_year):
        if start <= day <= end:
            return key
    # Unreachable for a well-formed calendar; guard loudly rather than return "".
    raise ValueError(f"no fiscal period contains {day.isoformat()}")
