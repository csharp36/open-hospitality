"""Reporting queries over the USALI staging and fact tables (P6).

Pure read-side: no schema changes, no writes, no printing. Two reports and an export:

- `summary_operating_statement` — an honest revenue-side SOS from the three fact
  tables for a single property, either for one business date or for an inclusive
  date range (financial and segment facts are SUMmed over the range; statistics use
  the latest business date in range, since MTD/YTD are as-of values).
- `coverage_report` — per-PMS-source mapping coverage and confidence: what the
  dictionaries claim (confidence / review-status breakdowns, the needs-review
  worklist) versus what actually staged (distinct trx codes, segment codes, and
  statistic labels, plus `mapping_exception` fallout). Financial coverage reads the
  `usali_mapping_dictionary` DB table — the same rows the transform actually mapped
  against; segments and statistics read their YAML dictionaries, which ARE their
  pipelines' source of truth.
- `export_rows` — flat, fully stringified fact-table rows (financial / statistics /
  segments) for ERP and BI hand-off; rendered by `rows_to_csv` / `rows_to_json` in
  `usali.render`.

Portal queries (P7):

- `line_transactions` — drill-through from one SOS financial line to the staged PMS
  transactions behind it: facts filtered by property + (major, sub_category,
  line_item) + business-date window, joined to their stage rows for the PMS code,
  description, and source file. Amounts come from the FACT rows — the numbers the
  SOS summed — so drill-through reconciles exactly to the line total.
- `list_properties` — distinct (property_id, pms_source) pairs with their first and
  last fact business dates; the portal's property picker.

CPA pack (P8):

- `cpa_pack` — the month-end hand-off for the CPA: a sales report (scheduled
  revenue grouped over the month; total == Σ per-day SOS TORs by construction),
  a tax report (pass-through taxes with their GL accounts plus the monthly Rooms
  revenue as the occupancy-tax base), and an A/R report (opening/closing ledger
  balances with movement, from `usali_ledger_balance_fact` kind=balance rows).
  Read-only, and lenient where the SOS is strict: an empty month is an empty
  pack, not an error — the CPA asks "what happened in July", and "nothing" is a
  valid answer.

Total Operating Revenue = sum of `usali_financial_fact.amount` where
`usali_schedule_id IS NOT NULL` (schedules 1-4 are revenue; taxes and settlements
carry a NULL schedule by design and are reported as pass-through/settlement sections).
"""

import logging
import re
from calendar import monthrange
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from usali.config import get_settings
from usali.models import (
    Base,
    Department,
    LaborStandard,
    MappingException,
    PayRun,
    PayRunLineProperty,
    PmsDailyFinancialStage,
    PmsDailySegmentStage,
    PmsDailyStatisticStage,
    Timecard,
    UsaliActualLaborFact,
    UsaliFinancialFact,
    UsaliLaborFact,
    UsaliLedgerBalanceFact,
    UsaliMappingDictionary,
    UsaliSegmentFact,
    UsaliStatisticFact,
)
from usali.segment_promote import SegmentMapping
from usali.stats_promote import MetricMapping

# Public on purpose: the QBO push (usali.qbo_push) partitions facts with the SAME
# major-category strings the SOS uses — one shared definition keeps the JE builder
# and this report from ever disagreeing about what counts as a tax or a settlement.
_LOG = logging.getLogger(__name__)

TAXES_MAJOR = "Taxes (Pass-Through)"
SETTLEMENTS_MAJOR = "Settlements"
# USALI revenue schedules: 1 Rooms, 2 Food & Beverage, 3 Other Operated, 4 Misc Income.
# Schedules 5-16 are expense schedules — they must never appear on the revenue-side SOS,
# so any other schedule id is rejected loudly rather than silently misclassified.
_OPERATED_SCHEDULES = frozenset({1, 2, 3})
_MISC_INCOME_SCHEDULE = 4
_ALLOWED_SCHEDULES = _OPERATED_SCHEDULES | {_MISC_INCOME_SCHEDULE}


class NoFactsError(ValueError):
    """No financial facts exist for the requested property/date window.

    Subclasses ValueError so existing `except ValueError` callers (the CLI) are
    unaffected; typed callers (the portal API) can distinguish "nothing there"
    (HTTP 404) from a bad request (HTTP 422) without matching on message text.
    """


@dataclass(frozen=True)
class SosLine:
    major: str
    sub_category: str
    line_item: str
    total: Decimal


@dataclass(frozen=True)
class DeptSection:
    sub_category: str
    lines: list[SosLine]
    total: Decimal


@dataclass(frozen=True)
class SegmentLine:
    segment: str
    rooms: Decimal
    room_revenue: Decimal


@dataclass(frozen=True)
class MetricRow:
    metric_code: str
    day: Decimal | None
    mtd: Decimal | None
    ytd: Decimal | None
    day_prior: Decimal | None
    mtd_prior: Decimal | None
    ytd_prior: Decimal | None


@dataclass(frozen=True)
class LaborLine:
    department: str
    hours: Decimal
    ot_hours: Decimal
    # ESTIMATE — meal premiums unpriced, superseded by Pillar C. None when the
    # department's labor in the window comes from a SINGLE distinct employee:
    # est_cost / effective-hours would re-derive that one employee's encrypted,
    # Payroll-Admin-gated pay rate, so the cost is SUPPRESSED (hours still show —
    # they are operational, not the rate).
    est_cost: Decimal | None


@dataclass(frozen=True)
class SickPayLine:
    """One department's sick pay for the window (E4) — the Schedule 14
    benefits side. Hours are operational and always show; `cost` derives at
    report time (regular rate on each day taken, primary placement) and is
    None when suppressed: one person's sick cost / their sick hours IS their
    rate, so the per-unit `_discloses` rule applies exactly as it does to
    wages."""

    department: str
    hours: Decimal
    cost: Decimal | None


@dataclass(frozen=True)
class LaborVarianceLine:
    department: str
    # Suppression is PER SIDE (see _labor_variance): est_cost is None when the
    # ESTIMATE side is single-employee with est > 0; actual_gross and
    # employer_burden are None together when the ACTUAL side is
    # single-employee with gross > 0 (burden ~ pct of gross); variance is None
    # when EITHER operand is hidden (else variance + the shown operand would
    # recover the hidden one).
    est_cost: Decimal | None
    actual_gross: Decimal | None
    employer_burden: Decimal | None
    variance: Decimal | None      # actual_gross - est_cost
    hours_actual: Decimal
    alert: bool


@dataclass(frozen=True)
class LaborVariance:
    """Estimate vs provider-actual labor for the processed pay periods
    intersecting the SOS window. Estimate/actual cover the FULL periods listed
    in `periods` (a period can extend past the window — the labels are the
    honest explanation). Deliberately OUTSIDE the revenue reconciliation."""

    lines: list[LaborVarianceLine]
    periods: list[str]
    # Totals are complementary PER COLUMN: each sums only the departments whose
    # value in that column is shown. variance_total == Σ shown line variances;
    # it equals actual_total - est_total only when nothing is suppressed.
    est_total: Decimal
    actual_total: Decimal
    variance_total: Decimal
    burden_total: Decimal
    alert: bool
    suppressed_departments: int  # departments with ANY hidden money column
    unpriced_hours: Decimal  # estimate hours with no rate inside the periods


@dataclass(frozen=True)
class SosReport:
    property_id: str
    pms_source: str
    business_date: date | None  # single-date mode
    date_from: date | None  # range mode
    date_to: date | None
    operated_departments: list[DeptSection]
    misc_income: list[SosLine]
    misc_income_total: Decimal
    total_operating_revenue: Decimal
    taxes: list[SosLine]
    taxes_total: Decimal
    settlements: list[SosLine]
    settlements_total: Decimal
    other: list[SosLine]
    other_total: Decimal
    rooms_segments: list[SegmentLine]
    statistics: list[MetricRow]
    # Pillar B3: promoted from approved timecards, unioned in. Schedule 14 =
    # estimated payroll expense; Schedule 15 = hours/OT/FTE. These are ESTIMATES
    # and are deliberately OUTSIDE the operating-revenue reconciliation (labor is
    # expense, not revenue).
    payroll_expense: list[LaborLine]
    payroll_expense_total: Decimal
    labor_hours_total: Decimal
    labor_ot_hours_total: Decimal
    labor_fte: Decimal | None
    # Count of departments whose est_cost was hidden (single-employee, rate-
    # derivation guard). Lets the statement honestly note cost is withheld rather
    # than silently drop it: payroll_expense_total EXCLUDES these departments.
    labor_suppressed_departments: int
    # Σ hours across facts booked at est_cost == 0 with real hours > 0 — an hourly
    # employee with no rate on file (the PUT guard forces rate > 0, so cost == 0
    # with hours > 0 means "no rate entered"). Derived from the RAW facts,
    # independent of per-department suppression.
    labor_unpriced_hours: Decimal
    # Pillar C3: estimate vs provider-actual for the pay periods intersecting the
    # window. None when no processed pay run touches it.
    labor_variance: LaborVariance | None
    # E4: sick pay taken in the window — the benefits side of Schedule 14.
    # Cost derives from dated rates at report time; suppressed departments'
    # cost is None and EXCLUDED from sick_pay_total (complementary, like
    # wages). sick_unpriced_hours: sick hours whose day had no rate in force
    # — real hours, underivable cost, named rather than zero-priced.
    # Defaulted (empty) so the render/export tests that hand-build reports
    # stay valid — the real constructor always fills them.
    sick_pay: list[SickPayLine] = field(default_factory=list)
    sick_pay_total: Decimal = Decimal("0.00")
    sick_suppressed_departments: int = 0
    sick_unpriced_hours: Decimal = Decimal("0.00")


def _grouped_lines(facts: Iterable[UsaliFinancialFact]) -> list[SosLine]:
    """SUM(amount) grouped by (major, sub_category, line_item), deterministically ordered."""
    sums: dict[tuple[str, str, str], Decimal] = {}
    for f in facts:
        key = (f.usali_major_category, f.usali_sub_category, f.usali_line_item)
        sums[key] = sums.get(key, Decimal("0")) + Decimal(str(f.amount))
    return [
        SosLine(major=major, sub_category=sub, line_item=line_item, total=total)
        for (major, sub, line_item), total in sorted(sums.items())
    ]


def _lines_total(lines: Iterable[SosLine]) -> Decimal:
    return sum((line.total for line in lines), Decimal("0"))


def _dept_sections(lines: list[SosLine]) -> list[DeptSection]:
    """Group operated-department lines by sub-category; Rooms leads, rest alphabetical."""
    by_sub: dict[str, list[SosLine]] = {}
    for line in lines:
        by_sub.setdefault(line.sub_category, []).append(line)
    return [
        DeptSection(sub_category=sub, lines=by_sub[sub], total=_lines_total(by_sub[sub]))
        for sub in sorted(by_sub, key=lambda s: (s != "Rooms", s))
    ]


def _rooms_segments(
    session: Session, property_id: str, date_from: date, date_to: date
) -> list[SegmentLine]:
    """DAY-period segment facts, SUMmed per segment over the window."""
    seg_facts = (
        session.execute(
            select(UsaliSegmentFact).where(
                UsaliSegmentFact.property_id == property_id,
                UsaliSegmentFact.period == "DAY",
                UsaliSegmentFact.business_date >= date_from,
                UsaliSegmentFact.business_date <= date_to,
            )
        )
        .scalars()
        .all()
    )
    sums: dict[str, tuple[Decimal, Decimal]] = {}
    for f in seg_facts:
        rooms, revenue = sums.get(f.usali_segment, (Decimal("0"), Decimal("0")))
        sums[f.usali_segment] = (
            rooms + Decimal(str(f.rooms)),
            revenue + Decimal(str(f.room_revenue)),
        )
    return [
        SegmentLine(segment=segment, rooms=rooms, room_revenue=revenue)
        for segment, (rooms, revenue) in sorted(sums.items())
    ]


def _statistics(
    session: Session, property_id: str, date_from: date, date_to: date
) -> list[MetricRow]:
    """Pivot statistic facts by metric: DAY/MTD/YTD plus prior-year columns.

    Statistics are as-of KPIs (MTD/YTD are cumulative), so the pivot always reads a
    single business date — the latest one in the window that has promoted facts.
    """
    stats_date = session.scalar(
        select(func.max(UsaliStatisticFact.business_date)).where(
            UsaliStatisticFact.property_id == property_id,
            UsaliStatisticFact.business_date >= date_from,
            UsaliStatisticFact.business_date <= date_to,
        )
    )
    if stats_date is None:
        return []
    stat_facts = (
        session.execute(
            select(UsaliStatisticFact).where(
                UsaliStatisticFact.property_id == property_id,
                UsaliStatisticFact.business_date == stats_date,
            )
        )
        .scalars()
        .all()
    )
    pivot: dict[str, dict[tuple[str, bool], Decimal]] = {}
    for f in stat_facts:
        pivot.setdefault(f.metric_code, {})[(f.period, f.is_prior_year)] = Decimal(str(f.value))
    return [
        MetricRow(
            metric_code=code,
            day=cells.get(("DAY", False)),
            mtd=cells.get(("MTD", False)),
            ytd=cells.get(("YTD", False)),
            day_prior=cells.get(("DAY", True)),
            mtd_prior=cells.get(("MTD", True)),
            ytd_prior=cells.get(("YTD", True)),
        )
        for code, cells in sorted(pivot.items())
    ]


def _labor_sections(
    session: Session, property_id: str, start: date, end: date
) -> tuple[list[LaborLine], Decimal, Decimal, Decimal, Decimal | None, int, Decimal]:
    """Union promoted labor facts into per-department Schedule 14/15 lines.

    Returns (lines, cost_total, hours_total, ot_hours_total, fte_estimate,
    suppressed_departments, unpriced_hours). FTE is a 40h-workweek equivalent
    prorated to the window; None when there are no hours.

    Rate-derivation guard: a department whose labor in the window comes from a
    single distinct PRICED employee has its est_cost SUPPRESSED (line est_cost=None and
    EXCLUDED from cost_total) — otherwise est_cost / effective-hours re-derives
    that one employee's encrypted, Payroll-Admin-gated pay rate. Hours still show.
    Suppression is COMPLEMENTARY: the solo cost appears nowhere, so
    `cost_total - Σ(shown lines)` cannot re-derive it either. The same rule applies
    to the NULL-department ("Unassigned") bucket. Distinct EMPLOYEES are counted,
    not distinct timecards: a wide window can give one employee two timecards, and
    counting timecards would read one person as two and wrongly skip suppression.
    PRICED means the fact contributes est_cost > 0: an exempt employee, or an
    hourly one with no rate on file, adds nothing to the disclosed number and so
    must not count toward the population that protects it.

    `unpriced_hours` (Σ hours where est_cost == 0 and hours > 0) is computed from
    the RAW facts, independent of the per-department suppression.
    """
    facts = session.execute(
        select(UsaliLaborFact).where(
            UsaliLaborFact.property_id == property_id,
            UsaliLaborFact.business_date >= start,
            UsaliLaborFact.business_date <= end,
        )
    ).scalars().all()
    if not facts:
        return [], Decimal("0"), Decimal("0"), Decimal("0"), None, 0, Decimal("0")

    # Unpriced hours: from the true fact cost, BEFORE any presentation-level
    # suppression (a suppressed solo dept still has its real fact cost).
    unpriced_hours = sum(
        (
            Decimal(str(f.hours))
            for f in facts
            if Decimal(str(f.est_cost)) == 0 and Decimal(str(f.hours)) > 0
        ),
        Decimal("0"),
    )

    # Resolve timecard_id -> employee_id so we can count DISTINCT EMPLOYEES per
    # department (UsaliLaborFact carries timecard_id but no employee_id).
    timecard_ids = {f.timecard_id for f in facts}
    tc_to_emp: dict[int, int] = {
        tc_id: emp_id
        for tc_id, emp_id in session.execute(
            select(Timecard.timecard_id, Timecard.employee_id).where(
                Timecard.timecard_id.in_(timecard_ids)
            )
        ).all()
    }

    agg: dict[int | None, tuple[Decimal, Decimal, Decimal]] = {}
    # PRICED POPULATION PER BUSINESS DATE, not per window. The window is
    # caller-controlled, so a window-wide count is a differencing oracle --
    # see `_discloses`. A day is the finest unit a caller can select.
    dept_day_employees: dict[int | None, dict[date, set[int]]] = {}
    for f in facts:
        hours, ot, cost = agg.get(f.department_id, (Decimal("0"), Decimal("0"), Decimal("0")))
        agg[f.department_id] = (
            hours + Decimal(str(f.hours)),
            ot + Decimal(str(f.ot_hours)),
            cost + Decimal(str(f.est_cost)),
        )
        emp_id = tc_to_emp.get(f.timecard_id)
        # PRICED population, not merely present. A fact with est_cost == 0 --
        # an FLSA-exempt employee (a salary is not a wage) or an hourly employee
        # with no rate on file -- contributes NOTHING to the disclosed cost, so
        # counting them lets a department with ONE priced employee escape
        # suppression and leak that person's rate via cost / hours.
        #
        # This is the D1 Critical: its fix was applied to the schedule
        # projection's gate but never to this one.
        if emp_id is not None and Decimal(str(f.est_cost)) > 0:
            dept_day_employees.setdefault(f.department_id, {}).setdefault(
                f.business_date, set()
            ).add(emp_id)

    names: dict[int | None, str] = {}
    for dept_id in agg:
        if dept_id is None:
            names[dept_id] = "Unassigned"
        else:
            dept = session.get(Department, dept_id)
            names[dept_id] = dept.name if dept is not None else f"department {dept_id}"

    lines: list[LaborLine] = []
    cost_total = Decimal("0")
    suppressed = 0
    for d, (h, ot, cost) in sorted(agg.items(), key=lambda kv: names[kv[0]]):
        # Every DAY carrying cost must have >= 2 distinct priced employees.
        # An empty map (no day carried cost at all) still suppresses, exactly as
        # the previous window-wide count did.
        if not _discloses(dept_day_employees.get(d, {})):
            suppressed += 1
            lines.append(LaborLine(department=names[d], hours=h, ot_hours=ot, est_cost=None))
        else:
            cost_total += cost
            lines.append(LaborLine(department=names[d], hours=h, ot_hours=ot, est_cost=cost))

    hours_total = sum((line.hours for line in lines), Decimal("0"))
    ot_total = sum((line.ot_hours for line in lines), Decimal("0"))

    window_days = (end - start).days + 1
    basis = Decimal(window_days) * Decimal("40") / Decimal("7")
    fte = (hours_total / basis).quantize(Decimal("0.01")) if basis > 0 else None

    return lines, cost_total, hours_total, ot_total, fte, suppressed, unpriced_hours


_TWO_DP = Decimal("0.01")


def _sick_pay(
    session: Session, property_id: str, start: date, end: date
) -> tuple[list[SickPayLine], Decimal, int, Decimal]:
    """Sick usage in the window, attributed to the PRIMARY placement's
    property and department on each day taken, priced from the dated rates
    (nothing stored — a closed window re-derives byte for byte).

    Disclosure follows `_discloses` per business DATE: the SOS window is
    caller-controlled, so a window-wide count is a differencing oracle (the
    E2 lesson, applied to this surface on day one). A rate refusal or a
    missing rate makes that person's day UNPRICED: the hours are named in
    sick_unpriced_hours, the cost stays underivable, and an unpriced person
    does not count toward the disclosure population.

    G3: the per-day resolution lives in `sick_days_taken` — the ONE
    derivation this section shares with the pay-run submission, so books
    and paychecks cannot disagree about the same hours. Voids net there
    (a voided usage was never taken; before G3 this section still derived
    cost from it — phantom benefits dollars).
    """
    from usali.sick_leave import sick_days_taken

    hours_by_dept: dict[int | None, Decimal] = {}
    cost_by_dept: dict[int | None, Decimal] = {}
    priced_by_dept: dict[int | None, dict[date, set[int]]] = {}
    unpriced = Decimal("0")
    for taken in sick_days_taken(session, start, end):
        if taken.property_id is None:
            # Unattributable: no (or ambiguous) placement on the day taken.
            # The API refuses such usage at entry, so this is reachable only
            # by out-of-band writes — but silence here would under-report a
            # money-bearing section (the review's F4: a dropped entry was in
            # NO property's hours, cost, or unpriced figure). Named
            # server-side; ids only.
            _LOG.warning(
                "sick usage on %s (employee_id=%s) has no resolvable "
                "primary placement; excluded from every SOS — repair the "
                "placement or void the entry",
                taken.day.isoformat(), taken.employee_id,
            )
            continue
        if taken.property_id != property_id:
            continue
        dept_id = taken.department_id
        hours_by_dept[dept_id] = (
            hours_by_dept.get(dept_id, Decimal("0")) + taken.hours
        )
        if taken.rate is None:
            unpriced += taken.hours
            continue
        cost_by_dept[dept_id] = (
            cost_by_dept.get(dept_id, Decimal("0")) + taken.hours * taken.rate
        )
        priced_by_dept.setdefault(dept_id, {}).setdefault(
            taken.day, set()
        ).add(taken.employee_id)

    def _name(dept_id: int | None) -> str:
        if dept_id is None:
            return "Unassigned"
        dept = session.get(Department, dept_id)
        return dept.name if dept is not None else f"department {dept_id}"

    lines: list[SickPayLine] = []
    total = Decimal("0")
    suppressed = 0
    for dept_id in sorted(hours_by_dept, key=lambda d: (_name(d), d or 0)):
        cost: Decimal | None
        if dept_id in cost_by_dept and _discloses(priced_by_dept.get(dept_id, {})):
            cost = cost_by_dept[dept_id].quantize(_TWO_DP)
            total += cost
        else:
            cost = None
            suppressed += 1
        lines.append(SickPayLine(
            department=_name(dept_id),
            hours=hours_by_dept[dept_id].quantize(_TWO_DP),
            cost=cost,
        ))
    return lines, total.quantize(_TWO_DP), suppressed, unpriced.quantize(_TWO_DP)


def _variance_alert(est: Decimal, variance: Decimal, alert_pct: int) -> bool:
    if est > 0:
        return abs(variance) * 100 >= est * alert_pct
    return variance > 0  # actual with no estimate baseline


_Unit = TypeVar("_Unit")


def _discloses(priced_by_unit: Mapping[_Unit, set[int]]) -> bool:
    """May this department's money be shown? Only if EVERY marginal unit that
    carries money was sourced from at least two distinct priced employees.

    ## Why per-unit and not per-window

    A static ">= 2 across the whole window" gate is a DIFFERENCING ORACLE
    whenever the window is caller-controlled, which `/api/sos?from=&to=` and the
    variance range both are. Two windows that each pass the static gate can be
    subtracted, and the difference is whatever lay between them:

        narrow (the 7th)      cost 337.36   hours 16.00
        wide   (7th-8th)      cost 522.72   hours 24.00
        delta                  185.36 /      8.00  =  23.17

    23.17 was one employee's exact hourly rate. Both windows held two priced
    people, so both disclosed; only the DIFFERENCE was solitary. Reproduced live
    against main before this rule existed.

    The same shape recurred one unit coarser in `_labor_variance`, where the
    marginal unit is a whole pay run rather than a day: 977.00 - 200.00 = 777.00
    was a single employee's gross.

    ## Why this rule closes it

    A caller selects whole units -- a business date on the SOS, a pay run on the
    variance report -- so the finest difference between any two windows is a sum
    of complete units. If every unit carrying money is itself a >= 2-person
    aggregate, then every such sum is a >= 2-person aggregate, and there is no
    query whose answer is one person's pay.

    Units with NO money are skipped: subtracting zero reveals nothing.

    ## What this costs, deliberately

    A department goes dark for any window containing a day (or run) where only
    one person was priced -- a single Sunday shift alone is enough to hide the
    whole month. That is a real loss of reporting detail, accepted because the
    alternative is publishing an individual's wage to anyone who can pass two
    date ranges. Hours are unaffected: Schedule 15 stays complete, and hours
    alone re-derive nothing without the cost.

    This is the FOURTH instance of the differencing class, after C3, D1 and D3.
    D3's lesson was recorded generally -- *any aggregate that varies with a
    caller-controlled parameter is a differencing oracle candidate* -- and was
    then applied only to the projection's `as_of`. Applying it to a second
    parameter is what this function is for, so a third does not need re-deriving.
    """
    funded = [emps for emps in priced_by_unit.values() if emps is not None]
    if not funded:
        return False
    return all(len(emps) >= 2 for emps in funded)


def _labor_variance(
    session: Session, property_id: str, start: date, end: date, *, alert_pct: int
) -> LaborVariance | None:
    """Estimate vs provider-actual labor over the PROCESSED pay runs whose
    periods intersect the [start, end] window (C3).

    Sums cover the runs' FULL periods (labeled in `periods`) — a period can
    extend past the window, so the block deliberately does not tie to the
    window-clipped estimate section. Periods CAN overlap (the unique constraint
    pins only period_start), so estimate facts are de-duplicated by primary key
    across the run loop — each fact counts at most once.

    Suppression is PER SIDE, each keyed to the money's OWN attribution —
    est_cost comes only from estimate facts (promote-time department, employee
    via timecard), actual gross/burden only from pay-run facts (fetch-time
    department snapshot on the pay line). A single union count would let a
    2-member union with a 1-person estimate show that solo person's est_cost
    (recoverable as a pay rate via the B3 section's shown hours), and
    symmetrically a 1-person actual's gross+burden:

    - est_cost hidden iff est > 0 and the ESTIMATE side has < 2 distinct
      employees (a dept with no estimate shows an honest 0 — nothing to leak);
    - actual_gross AND employer_burden hidden together iff gross > 0 and the
      ACTUAL side has < 2 distinct employees (burden is ~a fixed % of gross,
      so it would re-derive the solo rate);
    - variance hidden iff EITHER operand is (variance + one shown operand
      would recover the hidden one); hidden lines carry alert=False (an alert
      direction is itself a signal).

    Totals are complementary PER COLUMN: each total sums only the departments
    whose value in THAT column is shown, so subtraction cannot recover a
    hidden value. Consequently variance_total == Σ shown line variances, and
    it equals actual_total - est_total only when the suppression sets agree.
    `suppressed_departments` counts departments with ANY hidden column.
    """
    runs = session.execute(
        select(PayRun).where(
            PayRun.property_id == property_id,
            PayRun.status == "processed",
            PayRun.period_start <= end,
            PayRun.period_end >= start,
        ).order_by(PayRun.period_start)
    ).scalars().all()
    if not runs:
        return None
    run_ids = [r.pay_run_id for r in runs]
    periods = [f"{r.period_start.isoformat()}..{r.period_end.isoformat()}" for r in runs]

    # Actual side: department aggregates + distinct employees per department.
    actual: dict[int | None, tuple[Decimal, Decimal, Decimal]] = {}
    # PROPERTY-SCOPED, not merely run-scoped. Since E1, ONE pay run writes actual
    # facts for EVERY property the employee worked at -- the paycheck is issued by
    # their primary property but the cost belongs to both. Selecting purely by
    # pay_run_id therefore pulls the OTHER property's money onto this report,
    # under this property's headcount, and leaves the other property
    # under-reporting by the same amount. The estimate side below is already
    # property-scoped; the two must describe the same property.
    for f in session.execute(
        select(UsaliActualLaborFact).where(
            UsaliActualLaborFact.pay_run_id.in_(run_ids),
            UsaliActualLaborFact.property_id == property_id,
        )
    ).scalars():
        hours, gross, burden = actual.get(
            f.department_id, (Decimal("0"), Decimal("0"), Decimal("0"))
        )
        actual[f.department_id] = (
            hours + Decimal(str(f.hours)),
            gross + Decimal(str(f.gross)),
            burden + Decimal(str(f.employer_burden)),
        )

    # Actual-side distinct employees, keyed by each pay line's FETCH-TIME
    # department snapshot — the SAME attribution the aggregates above were
    # written under, so a later transfer cannot re-key the count away from the
    # money. The live Employee join is a fallback ONLY for legacy pre-C3 lines
    # whose snapshot is NULL (none in practice).
    # PER PAY RUN, for the same reason `_labor_sections` counts per day: the
    # window selects whole runs, so a union-wide count differences apart.
    actual_employees_by_dept: dict[int | None, dict[int, set[int]]] = {}
    for census in session.execute(
        select(PayRunLineProperty).where(
            PayRunLineProperty.pay_run_id.in_(run_ids),
            # PROPERTY-SCOPED. `PayRunLine` has no property dimension and its
            # `gross` is the employee's ENTIRE cross-property paycheck, so
            # counting it credited a headcount to the paying property even when
            # every dollar landed at the other one. A department where one
            # person actually worked then read as two, escaped suppression, and
            # published that person's exact gross and employer burden.
            PayRunLineProperty.property_id == property_id,
        )
    ).scalars():
        # PRICED population: a share of zero contributes nothing to the disclosed
        # figure, so it must not lift a solo department over the floor.
        if Decimal(str(census.gross or 0)) > 0:
            actual_employees_by_dept.setdefault(
                census.department_id, {}
            ).setdefault(census.pay_run_id, set()).add(census.employee_id)

    # Estimate side over the FULL run periods. Periods can OVERLAP (the unique
    # constraint pins only period_start), so track seen fact PKs — each
    # estimate fact counts at most once. Estimate-side employees are keyed by
    # the fact's promote-time department: exactly what est_cost is booked under.
    est: dict[int | None, Decimal] = {}
    # KEYED BY BUSINESS DATE, not by pay run — and that is the whole point.
    #
    # This side reads the SAME `UsaliLaborFact` rows that `_labor_sections`
    # publishes, and that function gates them per DAY. Gating them per RUN here
    # made the coarser report republish exactly what the finer one withheld: a
    # run whose fortnight holds two priced people passes a run-wide gate even
    # when one DAY inside it was solitary, so the estimate column disclosed a
    # figure the SOS section had just suppressed. Subtracting the two recovered
    # the individual:
    #
    #     variance est (7/7..7/12, per run)  907.28
    #     SOS section  (7/7..7/8,  per day)  656.00   hours 32.00
    #     SOS section  (7/7..7/12, per day)  None     hours 40.00
    #     (907.28 - 656.00) / (40.00 - 32.00) = 31.41 = one employee's rate
    #
    # THE RULE, generalised past `_discloses`'s own docstring: the unit must be
    # the finest grain ANY report exposes over a fact set, never the finest
    # grain THIS report happens to select. Two reports over one fact set are one
    # disclosure surface.
    #
    # The ACTUAL side below stays per-run, and legitimately: `UsaliActualLaborFact`
    # is period-grained, so no report publishes it any finer.
    est_employees_by_dept: dict[int | None, dict[date, set[int]]] = {}
    unpriced = Decimal("0")
    seen_fact_ids: set[int] = set()
    for run in runs:
        for lf in session.execute(
            select(UsaliLaborFact).where(
                UsaliLaborFact.property_id == property_id,
                UsaliLaborFact.business_date >= run.period_start,
                UsaliLaborFact.business_date <= run.period_end,
            )
        ).scalars():
            if lf.labor_fact_id in seen_fact_ids:
                continue
            seen_fact_ids.add(lf.labor_fact_id)
            est[lf.department_id] = est.get(lf.department_id, Decimal("0")) + Decimal(
                str(lf.est_cost)
            )
            if Decimal(str(lf.est_cost)) == 0 and Decimal(str(lf.hours)) > 0:
                unpriced += Decimal(str(lf.hours))
            card = session.get(Timecard, lf.timecard_id)
            # PRICED population, exactly as _labor_sections. `estimate` accumulates
            # over facts that carry cost; this gate must count the same facts. An
            # exempt employee or one with no rate on file contributes est_cost == 0
            # and must not lift a solo department over the suppression floor.
            # (The D1 Critical, fifth instance — fixed in _labor_sections first and
            # not carried here, which is exactly how the previous four survived.)
            if card is not None and Decimal(str(lf.est_cost)) > 0:
                est_employees_by_dept.setdefault(
                    lf.department_id, {}
                ).setdefault(lf.business_date, set()).add(card.employee_id)

    # G7 (money D): the ACTUAL side contains sick pay since G3, so an
    # estimate side without it showed a permanent phantom variance (alert
    # True) on every sick period — the "fix both sides together or
    # neither" shape from G plan decision 8, which had fixed only the
    # preflight estimate. The sick estimate re-derives from the ONE
    # derivation (`sick_days_taken`) over the runs' full periods,
    # attributed exactly as the SOS sick block attributes it. Its census
    # stays SEPARATE from the worked census: the SOS publishes worked and
    # sick money under different per-day gates, so a combined census here
    # would let a published SOS component be subtracted from this report
    # to recover the other (the cross-report differencing rule: two
    # reports over one fact set are one disclosure surface).
    from usali.sick_leave import sick_days_taken

    sick_est: dict[int | None, Decimal] = {}
    sick_employees_by_dept: dict[int | None, dict[date, set[int]]] = {}
    seen_sick: set[tuple[int, date]] = set()
    for run in runs:
        for taken in sick_days_taken(session, run.period_start, run.period_end):
            if (taken.employee_id, taken.day) in seen_sick:
                continue  # overlapping periods: each day counts once
            seen_sick.add((taken.employee_id, taken.day))
            if taken.property_id != property_id:
                continue  # another property's (or the SOS warning's)
            if taken.rate is None:
                unpriced += taken.hours
                continue
            sick_est[taken.department_id] = (
                sick_est.get(taken.department_id, Decimal("0"))
                + taken.hours * taken.rate
            )
            sick_employees_by_dept.setdefault(
                taken.department_id, {}
            ).setdefault(taken.day, set()).add(taken.employee_id)

    names: dict[int | None, str] = {}
    for dept_id in set(actual) | set(est) | set(sick_est):
        if dept_id is None:
            names[dept_id] = "Unassigned"
        else:
            dept = session.get(Department, dept_id)
            names[dept_id] = dept.name if dept is not None else f"department {dept_id}"

    lines: list[LaborVarianceLine] = []
    est_total = actual_total = variance_total = burden_total = Decimal("0")
    suppressed = 0
    for dept_id in sorted(set(actual) | set(est) | set(sick_est),
                          key=lambda d: names[d]):
        hours, gross, burden = actual.get(dept_id, (Decimal("0"), Decimal("0"), Decimal("0")))
        worked_est = est.get(dept_id, Decimal("0"))
        dept_sick_est = sick_est.get(dept_id, Decimal("0"))
        estimate = (worked_est + dept_sick_est).quantize(_TWO_DP)
        gross = gross.quantize(_TWO_DP)
        burden = burden.quantize(_TWO_DP)
        # Per-side suppression, each keyed to its own money's attribution (see
        # docstring). A zero column leaks nothing and shows honestly. The two
        # estimate COMPONENTS gate independently (see the sick block above).
        est_hidden = (
            worked_est > 0 and not _discloses(
                est_employees_by_dept.get(dept_id, {})
            )
        ) or (
            dept_sick_est > 0 and not _discloses(
                sick_employees_by_dept.get(dept_id, {})
            )
        )
        actual_hidden = gross > 0 and not _discloses(
            actual_employees_by_dept.get(dept_id, {})
        )
        variance = None if est_hidden or actual_hidden else gross - estimate
        if est_hidden or actual_hidden:
            suppressed += 1
        # Complementary totals PER COLUMN: only shown values are summed, so
        # `total - Σ shown` can never recover a hidden value.
        if not est_hidden:
            est_total += estimate
        if not actual_hidden:
            actual_total += gross
            burden_total += burden
        if variance is not None:
            variance_total += variance
        lines.append(LaborVarianceLine(
            department=names[dept_id],
            est_cost=None if est_hidden else estimate,
            actual_gross=None if actual_hidden else gross,
            employer_burden=None if actual_hidden else burden,
            variance=variance,
            hours_actual=hours,
            alert=(
                False if variance is None
                else _variance_alert(estimate, variance, alert_pct)
            ),
        ))

    return LaborVariance(
        lines=lines, periods=periods, est_total=est_total, actual_total=actual_total,
        variance_total=variance_total, burden_total=burden_total,
        alert=_variance_alert(est_total, variance_total, alert_pct),
        suppressed_departments=suppressed, unpriced_hours=unpriced,
    )


def summary_operating_statement(
    session: Session,
    *,
    property_id: str,
    business_date: date | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SosReport:
    """Build the Summary Operating Statement for one property.

    Exactly one mode must be given: `business_date` (single day) or both
    `date_from`/`date_to` (inclusive range; financial and segment facts are SUMmed,
    statistics come from the latest business date in the range).
    """
    if business_date is not None:
        if date_from is not None or date_to is not None:
            raise ValueError("pass either business_date or a date_from/date_to range, not both")
        start, end = business_date, business_date
    elif date_from is not None and date_to is not None:
        if date_from > date_to:
            raise ValueError(f"date_from {date_from} is after date_to {date_to}")
        start, end = date_from, date_to
    else:
        raise ValueError("pass business_date, or both date_from and date_to")

    facts = (
        session.execute(
            select(UsaliFinancialFact).where(
                UsaliFinancialFact.property_id == property_id,
                UsaliFinancialFact.business_date >= start,
                UsaliFinancialFact.business_date <= end,
            )
        )
        .scalars()
        .all()
    )
    if not facts:
        raise NoFactsError(
            f"no financial facts for property {property_id} between {start} and {end}"
        )
    sources = {f.pms_source for f in facts}
    if len(sources) != 1:
        raise ValueError(f"property {property_id} maps to multiple PMS sources: {sorted(sources)}")

    unexpected_schedules = sorted(
        {
            f.usali_schedule_id
            for f in facts
            if f.usali_schedule_id is not None and f.usali_schedule_id not in _ALLOWED_SCHEDULES
        }
    )
    if unexpected_schedules:
        raise ValueError(
            f"facts for property {property_id} carry unexpected USALI schedule ids "
            f"{unexpected_schedules}; the revenue-side SOS only classifies schedules "
            f"{sorted(_ALLOWED_SCHEDULES)} (plus NULL for taxes/settlements/other)"
        )

    operated = _grouped_lines(f for f in facts if f.usali_schedule_id in _OPERATED_SCHEDULES)
    misc = _grouped_lines(f for f in facts if f.usali_schedule_id == _MISC_INCOME_SCHEDULE)
    unscheduled = [f for f in facts if f.usali_schedule_id is None]
    taxes = _grouped_lines(f for f in unscheduled if f.usali_major_category == TAXES_MAJOR)
    settlements = _grouped_lines(
        f for f in unscheduled if f.usali_major_category == SETTLEMENTS_MAJOR
    )
    other = _grouped_lines(
        f for f in unscheduled if f.usali_major_category not in (TAXES_MAJOR, SETTLEMENTS_MAJOR)
    )
    total_operating_revenue = sum(
        (Decimal(str(f.amount)) for f in facts if f.usali_schedule_id is not None),
        Decimal("0"),
    )

    operated_departments = _dept_sections(operated)
    misc_income_total = _lines_total(misc)
    sections_total = sum((s.total for s in operated_departments), Decimal("0")) + misc_income_total
    # Self-check: the sections must reconcile exactly to the headline number. Amounts
    # are fixed-precision Numeric end-to-end, so exact Decimal equality is correct here.
    if sections_total != total_operating_revenue:
        raise ValueError(
            f"SOS sections do not reconcile for property {property_id}: operated + misc "
            f"{sections_total} != total operating revenue {total_operating_revenue}"
        )

    (payroll_expense, payroll_expense_total, labor_hours_total,
     labor_ot_hours_total, labor_fte, labor_suppressed_departments,
     labor_unpriced_hours) = _labor_sections(session, property_id, start, end)

    labor_variance = _labor_variance(
        session, property_id, start, end,
        alert_pct=get_settings().labor_variance_alert_pct,
    )

    (sick_pay, sick_pay_total, sick_suppressed_departments,
     sick_unpriced_hours) = _sick_pay(session, property_id, start, end)

    return SosReport(
        property_id=property_id,
        pms_source=next(iter(sources)),
        business_date=business_date,
        date_from=date_from,
        date_to=date_to,
        operated_departments=operated_departments,
        misc_income=misc,
        misc_income_total=misc_income_total,
        total_operating_revenue=total_operating_revenue,
        taxes=taxes,
        taxes_total=_lines_total(taxes),
        settlements=settlements,
        settlements_total=_lines_total(settlements),
        other=other,
        other_total=_lines_total(other),
        rooms_segments=_rooms_segments(session, property_id, start, end),
        statistics=_statistics(session, property_id, start, end),
        payroll_expense=payroll_expense,
        payroll_expense_total=payroll_expense_total,
        labor_hours_total=labor_hours_total,
        labor_ot_hours_total=labor_ot_hours_total,
        labor_fte=labor_fte,
        labor_suppressed_departments=labor_suppressed_departments,
        labor_unpriced_hours=labor_unpriced_hours,
        sick_pay=sick_pay,
        sick_pay_total=sick_pay_total,
        sick_suppressed_departments=sick_suppressed_departments,
        sick_unpriced_hours=sick_unpriced_hours,
        labor_variance=labor_variance,
    )


# --- Mapping coverage and confidence report -------------------------------------

# The staged segment TOTAL pseudo-code is the report's own reconciliation row, not a
# market/rate-plan code — it is never mapped and must not count as coverage.
_SEGMENT_TOTAL_CODE = "TOTAL"


@dataclass(frozen=True)
class NeedsReviewEntry:
    """One dictionary entry awaiting human review (the analyst worklist row)."""

    code: str
    line_item: str
    notes: str | None


@dataclass(frozen=True)
class FinancialCoverage:
    dictionary_entries: int
    by_confidence: dict[str, int]
    by_review_status: dict[str, int]
    needs_review: list[NeedsReviewEntry]
    staged_codes: int
    mapped_codes: int
    missing_codes: list[str]  # staged trx codes absent from the dictionary
    exception_count: int  # mapping_exception rows (unmapped rows the transform banked)
    gl_mapped: int  # dictionary entries carrying a GL account (QBO push-ready)
    gl_unmapped_codes: list[str]  # entries without one — the QBO push refuses these


@dataclass(frozen=True)
class SegmentCoverage:
    dictionary_entries: int
    needs_review: list[NeedsReviewEntry]  # line_item carries the USALI segment
    staged_codes: int
    mapped_codes: int
    unmapped_codes: list[str]  # strict promotion fails on these — must stay empty


@dataclass(frozen=True)
class StatisticsCoverage:
    dictionary_entries: int
    staged_labels: int
    mapped_labels: int
    unmapped_labels: list[str]  # lenient by design: informational, never an error


@dataclass(frozen=True)
class SourceCoverage:
    pms_source: str
    financial: FinancialCoverage
    segments: SegmentCoverage
    statistics: StatisticsCoverage


@dataclass(frozen=True)
class CoverageReport:
    sources: list[SourceCoverage]


def _load_yaml_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"no mapping dictionary at {path}")
    try:
        rows = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: {e}") from e
    if not isinstance(rows, list):
        raise ValueError(f"{path} must contain a YAML list of mapping entries")
    return rows


def _ordered_counts(values: Iterable[str], preferred: Sequence[str]) -> dict[str, int]:
    """Count occurrences, keyed in `preferred` order first, then any stragglers sorted."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ordered = [k for k in preferred if k in counts] + sorted(set(counts) - set(preferred))
    return {k: counts[k] for k in ordered}


def _financial_coverage(session: Session, source: str, edition: int) -> FinancialCoverage:
    """Coverage against `usali_mapping_dictionary` — the rows the transform mapped with.

    Scoped to one USALI edition exactly like `transform()`: the dictionary is keyed on
    (pms_source, pms_trx_code, usali_edition), so an edition-blind query would
    double-count codes once a second edition's rows coexist.
    """
    entries = (
        session.execute(
            select(UsaliMappingDictionary).where(
                UsaliMappingDictionary.pms_source == source,
                UsaliMappingDictionary.usali_edition == edition,
            )
        )
        .scalars()
        .all()
    )

    staged = set(
        session.scalars(
            select(PmsDailyFinancialStage.pms_trx_code)
            .where(PmsDailyFinancialStage.pms_source == source)
            .distinct()
        )
    )
    mapped_codes = {e.pms_trx_code for e in entries}
    gl_unmapped_codes = sorted(e.pms_trx_code for e in entries if e.gl_account_code is None)
    exception_count = session.scalar(
        select(func.count())
        .select_from(MappingException)
        .where(MappingException.pms_source == source)
    )
    assert exception_count is not None

    return FinancialCoverage(
        dictionary_entries=len(entries),
        by_confidence=_ordered_counts(
            (e.confidence for e in entries), preferred=("HIGH", "MEDIUM", "LOW")
        ),
        by_review_status=_ordered_counts(
            (e.review_status for e in entries),
            preferred=("reviewed", "needs-review", "unreviewed"),
        ),
        needs_review=[
            NeedsReviewEntry(code=e.pms_trx_code, line_item=e.usali_line_item, notes=e.notes)
            for e in sorted(entries, key=lambda e: e.pms_trx_code)
            if e.review_status == "needs-review"
        ],
        staged_codes=len(staged),
        mapped_codes=len(staged & mapped_codes),
        missing_codes=sorted(staged - mapped_codes),
        exception_count=exception_count,
        gl_mapped=len(entries) - len(gl_unmapped_codes),
        gl_unmapped_codes=gl_unmapped_codes,
    )


def _segment_coverage(session: Session, source: str, rows: list[dict[str, Any]]) -> SegmentCoverage:
    entries = [m for m in (SegmentMapping(**row) for row in rows) if m.source == source]
    staged = set(
        session.scalars(
            select(PmsDailySegmentStage.segment_code)
            .where(
                PmsDailySegmentStage.pms_source == source,
                PmsDailySegmentStage.segment_code != _SEGMENT_TOTAL_CODE,
            )
            .distinct()
        )
    )
    mapped_codes = {e.code for e in entries}
    return SegmentCoverage(
        dictionary_entries=len(entries),
        needs_review=[
            NeedsReviewEntry(code=e.code, line_item=e.segment, notes=e.notes)
            for e in sorted(entries, key=lambda e: e.code)
            if e.review_status == "needs-review"
        ],
        staged_codes=len(staged),
        mapped_codes=len(staged & mapped_codes),
        unmapped_codes=sorted(staged - mapped_codes),
    )


def _statistics_coverage(
    session: Session, source: str, rows: list[dict[str, Any]]
) -> StatisticsCoverage:
    entries = [m for m in (MetricMapping(**row) for row in rows) if m.source == source]
    staged = set(
        session.scalars(
            select(PmsDailyStatisticStage.metric_label)
            .where(PmsDailyStatisticStage.pms_source == source)
            .distinct()
        )
    )
    mapped_labels = {e.label for e in entries}
    return StatisticsCoverage(
        dictionary_entries=len(entries),
        staged_labels=len(staged),
        mapped_labels=len(staged & mapped_labels),
        unmapped_labels=sorted(staged - mapped_labels),
    )


def coverage_report(
    session: Session,
    *,
    edition: int = 12,
    segments_path: str | Path = "mapping/segments.yaml",
    statistics_path: str | Path = "mapping/statistics.yaml",
) -> CoverageReport:
    """Build the mapping coverage and confidence report, one section per PMS source.

    Sources are discovered from the staging tables (whatever actually ingested), so an
    empty database yields an empty report. Per source: the financial dictionary (the
    `usali_mapping_dictionary` DB table scoped to `edition`, the same rows the
    transform mapped against) is broken down by confidence and review status with a
    needs-review worklist;
    staged distinct trx codes / segment codes / statistic labels are compared against
    their dictionaries (segments and statistics read YAML, which IS their pipelines'
    source of truth). Financial and segment gaps are actionable (the pipeline banks
    exceptions or fails strictly); statistics matching is lenient by design, so its
    leftovers are informational.
    """
    sources = sorted(
        set(session.scalars(select(PmsDailyFinancialStage.pms_source).distinct()))
        | set(session.scalars(select(PmsDailySegmentStage.pms_source).distinct()))
        | set(session.scalars(select(PmsDailyStatisticStage.pms_source).distinct()))
    )
    segment_rows = _load_yaml_rows(Path(segments_path))
    statistic_rows = _load_yaml_rows(Path(statistics_path))
    return CoverageReport(
        sources=[
            SourceCoverage(
                pms_source=source,
                financial=_financial_coverage(session, source, edition),
                segments=_segment_coverage(session, source, segment_rows),
                statistics=_statistics_coverage(session, source, statistic_rows),
            )
            for source in sources
        ]
    )


# --- Flat fact-table exports for ERP/BI ------------------------------------------

ExportTable = Literal["financial", "statistics", "segments"]

# Contract: every model listed here must carry business_date and property_id columns
# (export_rows filters and orders on them).
_EXPORT_MODELS: dict[str, type[Base]] = {
    "financial": UsaliFinancialFact,
    "statistics": UsaliStatisticFact,
    "segments": UsaliSegmentFact,
}


def _export_model(table: ExportTable) -> type[Base]:
    model = _EXPORT_MODELS.get(table)
    if model is None:
        raise ValueError(
            f"unknown export table {table!r}; expected one of {sorted(_EXPORT_MODELS)}"
        )
    return model


def export_columns(table: ExportTable) -> list[str]:
    """Column names for one export table, in schema declaration order.

    This is the CSV header contract: pass it to `render.rows_to_csv` as `fieldnames`
    so an export that selects zero rows still emits a header-only file.
    """
    return [col.name for col in _export_model(table).__table__.columns]


def _export_value(value: object) -> str:
    """Stringify one cell: NULL -> "", bools lowercase, dates ISO, Decimals plain str."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):  # covers datetime too (subclass); isoformat either way
        return value.isoformat()
    return str(value)


def export_rows(
    session: Session,
    table: ExportTable,
    *,
    date_from: date,
    date_to: date,
    property_id: str | None = None,
) -> list[dict[str, str]]:
    """Export one fact table as flat, fully stringified rows for ERP/BI hand-off.

    Selects `usali_financial_fact` / `usali_statistic_fact` / `usali_segment_fact`
    filtered to the inclusive `date_from`..`date_to` business-date window (and to one
    property when `property_id` is given). Each row is a dict over every table column
    in schema declaration order: Decimals as plain strings (exact, never floats),
    dates ISO, NULLs as empty strings, booleans as "true"/"false". Rows are ordered
    by business date then primary key, so re-exports are byte-stable.
    """
    model = _export_model(table)
    if date_from > date_to:
        raise ValueError(f"date_from {date_from} is after date_to {date_to}")

    columns = model.__table__.columns
    query = (
        select(model)
        .where(
            columns["business_date"] >= date_from,
            columns["business_date"] <= date_to,
        )
        .order_by(columns["business_date"], *model.__table__.primary_key)
    )
    if property_id is not None:
        query = query.where(columns["property_id"] == property_id)

    return [
        {col.name: _export_value(getattr(row, col.name)) for col in columns}
        for row in session.execute(query).scalars()
    ]


# --- Portal queries (drill-through and property listing) --------------------------


@dataclass(frozen=True)
class StagedTxn:
    """One staged PMS transaction behind an SOS line (drill-through row)."""

    stage_id: int
    pms_source: str
    business_date: date
    pms_trx_code: str
    pms_trx_desc: str | None
    amount: Decimal
    source_file: str


@dataclass(frozen=True)
class PropertyInfo:
    property_id: str
    pms_source: str
    first_date: date
    last_date: date


def line_transactions(
    session: Session,
    *,
    property_id: str,
    major: str,
    sub_category: str,
    line_item: str,
    date_from: date,
    date_to: date,
) -> list[StagedTxn]:
    """Drill through one SOS financial line to the staged PMS transactions behind it.

    Selects `usali_financial_fact` rows for the property and (major, sub_category,
    line_item) triple within the inclusive business-date window, joined to their
    `pms_daily_financial_stage` rows for the PMS transaction code, description, and
    source file. `amount` comes from the FACT row — the number the SOS summed —
    since the stage's `raw_amount` may differ in sign convention. Ordered by
    (business_date, stage_id) so re-reads are stable. An unknown line simply
    returns an empty list: drill-through of nothing is nothing, not an error.
    """
    if date_from > date_to:
        raise ValueError(f"date_from {date_from} is after date_to {date_to}")

    rows = session.execute(
        select(UsaliFinancialFact, PmsDailyFinancialStage)
        .join(
            PmsDailyFinancialStage,
            UsaliFinancialFact.stage_id == PmsDailyFinancialStage.stage_id,
        )
        .where(
            UsaliFinancialFact.property_id == property_id,
            UsaliFinancialFact.usali_major_category == major,
            UsaliFinancialFact.usali_sub_category == sub_category,
            UsaliFinancialFact.usali_line_item == line_item,
            UsaliFinancialFact.business_date >= date_from,
            UsaliFinancialFact.business_date <= date_to,
        )
        .order_by(UsaliFinancialFact.business_date, UsaliFinancialFact.stage_id)
    ).all()
    return [
        StagedTxn(
            stage_id=stage.stage_id,
            pms_source=fact.pms_source,
            business_date=fact.business_date,
            pms_trx_code=stage.pms_trx_code,
            pms_trx_desc=stage.pms_trx_desc,
            amount=Decimal(str(fact.amount)),
            source_file=stage.source_file,
        )
        for fact, stage in rows
    ]


def list_properties(session: Session) -> list[PropertyInfo]:
    """List every (property_id, pms_source) with promoted financial facts.

    First/last dates are the min/max fact business dates — the window the portal
    can meaningfully query. Ordered by property_id; empty database yields [].
    """
    rows = session.execute(
        select(
            UsaliFinancialFact.property_id,
            UsaliFinancialFact.pms_source,
            func.min(UsaliFinancialFact.business_date),
            func.max(UsaliFinancialFact.business_date),
        )
        .group_by(UsaliFinancialFact.property_id, UsaliFinancialFact.pms_source)
        .order_by(UsaliFinancialFact.property_id, UsaliFinancialFact.pms_source)
    ).all()
    return [
        PropertyInfo(
            property_id=property_id,
            pms_source=pms_source,
            first_date=first_date,
            last_date=last_date,
        )
        for property_id, pms_source, first_date, last_date in rows
    ]


# --- CPA monthly pack (sales, tax, A/R) -------------------------------------------

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def month_bounds(month: str) -> tuple[date, date]:
    """Inclusive (first, last) days of a 'YYYY-MM' month; loud on bad input."""
    if not _MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    year, month_no = int(month[:4]), int(month[5:7])
    if not 1 <= month_no <= 12:
        raise ValueError(f"month must be YYYY-MM with a real month, got {month!r}")
    return date(year, month_no, 1), date(year, month_no, monthrange(year, month_no)[1])


@dataclass(frozen=True)
class SalesLine:
    major: str
    sub_category: str
    line_item: str
    mtd_amount: Decimal
    day_count: int  # distinct business dates in the month with facts on this line


@dataclass(frozen=True)
class SalesReport:
    lines: list[SalesLine]
    total_operating_revenue: Decimal  # == Σ per-day SOS TORs (same fact predicate)


@dataclass(frozen=True)
class TaxLine:
    line_item: str
    gl_account_code: str | None  # the liability account the QBO push posts this tax to
    mtd_amount: Decimal


@dataclass(frozen=True)
class TaxReport:
    lines: list[TaxLine]
    taxes_total: Decimal
    room_revenue_base: Decimal  # monthly Rooms revenue — the occupancy-tax base


@dataclass(frozen=True)
class ArLine:
    """One ledger's month movement, fields in chronological order (opening first).

    `opening_balance` is the FIRST reported balance IN the month, not the prior
    month's close — with daily trial-balance ingestion the two coincide, but after
    an ingestion gap the opening is simply the earliest date that reported.
    Downstream mirrors of this shape (e.g. portal TypeScript types) must carry
    the same note.
    """

    ledger_code: str
    ledger_name: str
    opening_balance: Decimal  # earliest balance fact in the month (see docstring)
    closing_balance: Decimal  # latest balance fact in the month
    movement: Decimal  # closing − opening


@dataclass(frozen=True)
class ArReport:
    balances: list[ArLine]


@dataclass(frozen=True)
class CpaPack:
    property_id: str
    pms_source: str
    month: str  # "YYYY-MM"
    sales: SalesReport
    taxes: TaxReport
    ar: ArReport


def _sales_report(facts: Sequence[UsaliFinancialFact]) -> SalesReport:
    """Scheduled (revenue) facts grouped by (major, sub, line_item) over the month.

    The total uses the exact predicate the SOS uses for Total Operating Revenue
    (`usali_schedule_id IS NOT NULL`), so the monthly total equals the sum of the
    per-day SOS TORs by construction.
    """
    scheduled = [f for f in facts if f.usali_schedule_id is not None]
    sums: dict[tuple[str, str, str], Decimal] = {}
    days: dict[tuple[str, str, str], set[date]] = {}
    for f in scheduled:
        key = (f.usali_major_category, f.usali_sub_category, f.usali_line_item)
        sums[key] = sums.get(key, Decimal("0")) + Decimal(str(f.amount))
        days.setdefault(key, set()).add(f.business_date)
    return SalesReport(
        lines=[
            SalesLine(
                major=major,
                sub_category=sub,
                line_item=line_item,
                mtd_amount=total,
                day_count=len(days[(major, sub, line_item)]),
            )
            for (major, sub, line_item), total in sorted(sums.items())
        ],
        total_operating_revenue=sum(
            (Decimal(str(f.amount)) for f in scheduled), Decimal("0")
        ),
    )


def _tax_report(facts: Sequence[UsaliFinancialFact]) -> TaxReport:
    """NULL-schedule tax facts by (line_item, GL); Rooms revenue as the occupancy base."""
    tax_facts = [
        f
        for f in facts
        if f.usali_schedule_id is None and f.usali_major_category == TAXES_MAJOR
    ]
    sums: dict[tuple[str, str | None], Decimal] = {}
    for f in tax_facts:
        key = (f.usali_line_item, f.gl_account_code)
        sums[key] = sums.get(key, Decimal("0")) + Decimal(str(f.amount))
    room_revenue_base = sum(
        (
            Decimal(str(f.amount))
            for f in facts
            if f.usali_schedule_id in _OPERATED_SCHEDULES and f.usali_sub_category == "Rooms"
        ),
        Decimal("0"),
    )
    return TaxReport(
        lines=[
            TaxLine(line_item=line_item, gl_account_code=gl, mtd_amount=total)
            for (line_item, gl), total in sorted(
                sums.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
            )
        ],
        taxes_total=sum((Decimal(str(f.amount)) for f in tax_facts), Decimal("0")),
        room_revenue_base=room_revenue_base,
    )


def _ar_report(ledger_facts: Sequence[UsaliLedgerBalanceFact]) -> ArReport:
    """Opening/closing per ledger from kind=balance facts within the month.

    Balances are as-of values, never summed: closing is the ledger's latest fact in
    the month, opening its earliest (per ledger — a code absent on some dates still
    gets its own true first/last). A property with no ledger facts (e.g. a PMS that
    exports no trial-balance ledger block) yields an empty balances list.
    """
    by_code: dict[str, list[UsaliLedgerBalanceFact]] = {}
    for f in ledger_facts:
        by_code.setdefault(f.ledger_code, []).append(f)
    balances: list[ArLine] = []
    for code in sorted(by_code):
        rows = sorted(by_code[code], key=lambda f: f.business_date)
        opening = Decimal(str(rows[0].amount))
        closing = Decimal(str(rows[-1].amount))
        balances.append(
            ArLine(
                ledger_code=code,
                ledger_name=rows[-1].ledger_name,
                opening_balance=opening,
                closing_balance=closing,
                movement=closing - opening,
            )
        )
    return ArReport(balances=balances)


def cpa_pack(session: Session, *, property_id: str, month: str) -> CpaPack:
    """Build the CPA monthly pack (sales, tax, A/R) for one property.

    `month` is "YYYY-MM" (loud ValueError otherwise). Unlike the SOS, an empty
    month is a clean empty pack, not an error — the PMS source then falls back
    to a property-wide lookup so the header still says which system this is.
    An entirely unknown property is a loud ValueError, never a plausible-looking
    zero pack (a typo'd --property must not read as "no activity this month").
    """
    start, end = month_bounds(month)

    facts = (
        session.execute(
            select(UsaliFinancialFact).where(
                UsaliFinancialFact.property_id == property_id,
                UsaliFinancialFact.business_date >= start,
                UsaliFinancialFact.business_date <= end,
            )
        )
        .scalars()
        .all()
    )
    ledger_facts = (
        session.execute(
            select(UsaliLedgerBalanceFact).where(
                UsaliLedgerBalanceFact.property_id == property_id,
                UsaliLedgerBalanceFact.kind == "balance",
                UsaliLedgerBalanceFact.business_date >= start,
                UsaliLedgerBalanceFact.business_date <= end,
            )
        )
        .scalars()
        .all()
    )

    sources = {f.pms_source for f in facts} | {f.pms_source for f in ledger_facts}
    if not sources:
        # Property-wide fallback checks financial facts only: every supported PMS
        # produces financial facts, so a property with ONLY ledger facts (which
        # would blank here) can't currently exist — acknowledged edge, not handled.
        sources = set(
            session.scalars(
                select(UsaliFinancialFact.pms_source)
                .where(UsaliFinancialFact.property_id == property_id)
                .distinct()
            )
        )
    if not sources:
        raise ValueError(f"unknown property {property_id!r}: no facts in any month")
    if len(sources) > 1:
        raise ValueError(
            f"property {property_id} maps to multiple PMS sources: {sorted(sources)}"
        )

    return CpaPack(
        property_id=property_id,
        pms_source=next(iter(sources)),
        month=month,
        sales=_sales_report(facts),
        taxes=_tax_report(facts),
        ar=_ar_report(ledger_facts),
    )


# --- Labor analytics (payroll dashboard) -------------------------------------
#
# A retrospective read of what labor actually cost, in the terms a GM decides
# staffing with: cost against revenue, cost and hours against rooms sold,
# overtime concentration, and actual hours against the department's own
# standard.
#
# EVERY MONEY FIGURE HERE COMES OUT OF `_labor_sections`, per day and again for
# the window. That is deliberate and load-bearing: the suppression rule
# (`_discloses`) is per-day precisely because a caller-controlled window is a
# differencing oracle, and a series of days IS a caller-controlled window taken
# to its limit. Re-implementing the aggregate here with one SUM would have
# published exactly what that rule exists to withhold.
#
# It costs one `_labor_sections` call per day. That is the trade taken
# knowingly: one implementation of a disclosure rule, not two.


@dataclass(frozen=True)
class LaborDay:
    business_date: date
    hours: Decimal
    ot_hours: Decimal
    est_cost: Decimal          # disclosed departments only, like the statement
    rooms_occupied: Decimal | None
    revenue: Decimal | None
    # Hours per department on THIS day. Hours only, never cost: hours are
    # operational and are never suppressed (a solo department still reports
    # them), whereas a per-day per-department COST would hand back exactly what
    # `_discloses` withholds -- one person's rate, recoverable as cost / hours.
    department_hours: dict[str, Decimal]


@dataclass(frozen=True)
class LaborDepartment:
    department: str
    hours: Decimal
    ot_hours: Decimal
    est_cost: Decimal | None   # None = suppressed (fewer than two priced employees)
    target_hours: Decimal | None


@dataclass(frozen=True)
class LaborAnalytics:
    property_id: str
    date_from: date
    date_to: date
    days: list[LaborDay]
    departments: list[LaborDepartment]
    hours_total: Decimal
    ot_hours_total: Decimal
    cost_total: Decimal
    revenue_total: Decimal
    rooms_total: Decimal
    fte: Decimal | None
    suppressed_departments: int
    unpriced_hours: Decimal


def _rooms_by_day(
    session: Session, property_id: str, start: date, end: date
) -> dict[date, Decimal]:
    """Rooms actually sold per day, from the promoted DAY statistic."""
    rows = session.execute(
        select(UsaliStatisticFact.business_date, UsaliStatisticFact.value).where(
            UsaliStatisticFact.property_id == property_id,
            UsaliStatisticFact.business_date >= start,
            UsaliStatisticFact.business_date <= end,
            UsaliStatisticFact.metric_code == "ROOMS_OCCUPIED",
            UsaliStatisticFact.period == "DAY",
            UsaliStatisticFact.is_prior_year.is_(False),
        )
    ).all()
    # Last write wins on a duplicate, matching `_statistics`' pivot — statistics
    # are as-of KPIs, never summed.
    return {d: Decimal(str(v)) for d, v in rows}


def _revenue_by_day(
    session: Session, property_id: str, start: date, end: date
) -> dict[date, Decimal]:
    """Total operating revenue per day — the SAME predicate the statement uses
    (`usali_schedule_id IS NOT NULL`), so the two can never disagree."""
    rows = session.execute(
        select(
            UsaliFinancialFact.business_date, func.sum(UsaliFinancialFact.amount)
        )
        .where(
            UsaliFinancialFact.property_id == property_id,
            UsaliFinancialFact.business_date >= start,
            UsaliFinancialFact.business_date <= end,
            UsaliFinancialFact.usali_schedule_id.is_not(None),
        )
        .group_by(UsaliFinancialFact.business_date)
    ).all()
    return {d: Decimal(str(total)) for d, total in rows}


def _standard_targets(
    session: Session,
    property_id: str,
    rooms: Mapping[date, Decimal],
    days: Sequence[date],
) -> dict[str, Decimal]:
    """Target hours per department over the window, from its labor standard.

    Retrospective, so `minutes_per_occupied_room` multiplies the rooms ACTUALLY
    SOLD rather than the forecast — the question here is "given the demand that
    turned up, were these the right hours", and answering it against a forecast
    would score the forecast instead of the staffing. A day with no promoted
    room count contributes nothing: absence is not zero demand (the same rule
    `week_targets` applies to a missing forecast).
    """
    targets: dict[str, Decimal] = {}
    for std in session.execute(
        select(LaborStandard).where(LaborStandard.property_id == property_id)
    ).scalars():
        dept = session.get(Department, std.department_id)
        name = dept.name if dept is not None else f"department {std.department_id}"
        total = Decimal("0")
        for d in days:
            if std.basis == "fixed_hours_per_day":
                total += Decimal(str(std.value))
            elif (sold := rooms.get(d)) is not None:
                total += Decimal(str(std.value)) * sold / 60
        targets[name] = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return targets


def labor_analytics(
    session: Session, property_id: str, start: date, end: date
) -> LaborAnalytics:
    """Labor cost and hours over a window, per day and per department."""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    rooms = _rooms_by_day(session, property_id, start, end)
    revenue = _revenue_by_day(session, property_id, start, end)

    day_rows: list[LaborDay] = []
    for d in days:
        # The per-day lines were already being computed and thrown away. They
        # are the ONLY honest source for a per-department daily series: sharing
        # out the day total by each department's share of the window would give
        # every department the same shape and call it a trend.
        day_lines, cost, hours, ot, _fte, _suppressed, _unpriced = _labor_sections(
            session, property_id, d, d
        )
        day_rows.append(LaborDay(
            business_date=d,
            hours=hours,
            ot_hours=ot,
            est_cost=cost,
            rooms_occupied=rooms.get(d),
            revenue=revenue.get(d),
            department_hours={line.department: line.hours for line in day_lines},
        ))

    lines, cost_total, hours_total, ot_total, fte, suppressed, unpriced = _labor_sections(
        session, property_id, start, end
    )
    targets = _standard_targets(session, property_id, rooms, days)
    departments = [
        LaborDepartment(
            department=line.department,
            hours=line.hours,
            ot_hours=line.ot_hours,
            est_cost=line.est_cost,
            target_hours=targets.get(line.department),
        )
        for line in lines
    ]
    # A department with a standard but no hours worked is still a finding — an
    # unstaffed target is exactly what a GM needs to see.
    staffed = {line.department for line in lines}
    for name, target in sorted(targets.items()):
        if name not in staffed:
            departments.append(LaborDepartment(
                department=name, hours=Decimal("0"), ot_hours=Decimal("0"),
                est_cost=Decimal("0"), target_hours=target,
            ))

    return LaborAnalytics(
        property_id=property_id,
        date_from=start,
        date_to=end,
        days=day_rows,
        departments=departments,
        hours_total=hours_total,
        ot_hours_total=ot_total,
        cost_total=cost_total,
        revenue_total=sum(revenue.values(), Decimal("0")),
        rooms_total=sum(rooms.values(), Decimal("0")),
        fte=fte,
        suppressed_departments=suppressed,
        unpriced_hours=unpriced,
    )
