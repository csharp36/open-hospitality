"""Core performance statistics (issue #9): occupancy, ADR, RevPAR, TRevPAR, and
labor productivity, recomputed from primitives over a date range or a fiscal
period, with prior-period/prior-year comparisons and operator trend bases.

Pure functions over the promoted fact tables. Room/revenue metrics carry no
per-employee money and are ungated; labor-COST metrics compose with the
reporting._discloses per-day guard (never a fresh SUM) so a caller-controlled
window cannot be a differencing oracle. Denominators come from
inventory.rooms_available (fail-loud). #26 adds the expense side (GOPPAR/CPOR).
"""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.inventory import InventoryNotConfigured, rooms_available, total_rooms_on
from usali.models import IngestionCoverage, UsaliSegmentFact, UsaliStatisticFact
from usali.reporting import _labor_sections

# A day needs its statistics report; OPERA emits manager_flash, AUTOCLERK
# manager_report — a property uses one PMS, so either satisfies the requirement.
REQUIRED_STATISTICS_REPORTS: frozenset[str] = frozenset({"manager_flash", "manager_report"})


def _stat_by_day(
    session: Session, property_id: str, start: date, end: date, metric_code: str
) -> dict[date, Decimal]:
    """A promoted DAY statistic per business date (last write wins on a dup, as
    statistics are as-of KPIs, never summed — the _rooms_by_day convention)."""
    rows = session.execute(
        select(UsaliStatisticFact.business_date, UsaliStatisticFact.value).where(
            UsaliStatisticFact.property_id == property_id,
            UsaliStatisticFact.business_date >= start,
            UsaliStatisticFact.business_date <= end,
            UsaliStatisticFact.metric_code == metric_code,
            UsaliStatisticFact.period == "DAY",
            UsaliStatisticFact.is_prior_year.is_(False),
        )
    ).all()
    return {d: Decimal(str(v)) for d, v in rows}


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def complete_days(session: Session, property_id: str, start: date, end: date) -> set[date]:
    """The business dates in [start, end] with a statistics report landed (see
    REQUIRED_STATISTICS_REPORTS) and an in-force room count (so rooms_available
    resolves). Trend bases and comparisons consume only these days."""
    have: dict[date, set[str]] = {}
    for d, rt in session.execute(
        select(IngestionCoverage.business_date, IngestionCoverage.report_type).where(
            IngestionCoverage.property_id == property_id,
            IngestionCoverage.business_date >= start,
            IngestionCoverage.business_date <= end,
        )
    ).all():
        have.setdefault(d, set()).add(rt)
    out: set[date] = set()
    for d in _days(start, end):
        if not (have.get(d, set()) & REQUIRED_STATISTICS_REPORTS):
            continue
        try:
            total_rooms_on(session, property_id, d)
        except InventoryNotConfigured:
            continue
        out.add(d)
    return out


_COMP_HOUSE_SEGMENTS = ("COMPLIMENTARY", "HOUSE_USE")  # promote stores canonical kinds


class AdrBasisUnavailable(Exception):
    """`exclude_comp_house` was requested but a day in the window has no segment
    data to net comp/house-use from — refuse rather than silently not-excluding
    (adr-010)."""


def _comp_house_rooms_by_day(
    session: Session, property_id: str, start: date, end: date
) -> dict[date, Decimal]:
    rows = session.execute(
        select(UsaliSegmentFact.business_date, UsaliSegmentFact.rooms).where(
            UsaliSegmentFact.property_id == property_id,
            UsaliSegmentFact.business_date >= start,
            UsaliSegmentFact.business_date <= end,
            UsaliSegmentFact.period == "DAY",
            UsaliSegmentFact.usali_segment.in_(_COMP_HOUSE_SEGMENTS),
        )
    ).all()
    out: dict[date, Decimal] = {}
    for d, rooms in rows:
        out[d] = out.get(d, Decimal("0")) + Decimal(str(rooms))
    return out


def _segment_days(
    session: Session, property_id: str, start: date, end: date
) -> set[date]:
    rows = session.execute(
        select(UsaliSegmentFact.business_date).where(
            UsaliSegmentFact.property_id == property_id,
            UsaliSegmentFact.business_date >= start,
            UsaliSegmentFact.business_date <= end,
            UsaliSegmentFact.period == "DAY",
        ).distinct()
    ).scalars().all()
    return set(rows)


def adr_rooms_sold(
    session: Session, property_id: str, start: date, end: date, basis: str
) -> Decimal:
    """Rooms sold on the ADR basis over the window. `as_reported` = Σ
    ROOMS_OCCUPIED; `exclude_comp_house` subtracts segment comp+house-use rooms,
    refusing (AdrBasisUnavailable) if any occupied day lacks segment data."""
    rooms = _stat_by_day(session, property_id, start, end, "ROOMS_OCCUPIED")
    total = sum(rooms.values(), Decimal("0"))
    if basis == "as_reported":
        return total
    seg_days = _segment_days(session, property_id, start, end)
    for d in rooms:
        if rooms[d] > 0 and d not in seg_days:
            raise AdrBasisUnavailable(
                f"{property_id} is set to exclude comp/house-use from ADR, but "
                f"{d.isoformat()} has occupied rooms and no market-segment data to "
                "net them from — ingest the segment statistics or switch the ADR basis"
            )
    comp_house = _comp_house_rooms_by_day(session, property_id, start, end)
    return total - sum(comp_house.values(), Decimal("0"))


_Q4 = Decimal("0.0001")


def _ratio(num: Decimal, den: Decimal) -> Decimal | None:
    if den == 0:
        return None
    return (num / den).quantize(_Q4)


@dataclass(frozen=True)
class CoreMetrics:
    start: date
    end: date
    rooms_available: Decimal
    rooms_sold: Decimal
    adr_rooms_sold: Decimal
    room_revenue: Decimal
    total_revenue: Decimal
    occupancy: Decimal | None
    adr: Decimal | None
    revpar: Decimal | None
    trevpar: Decimal | None
    adr_room_basis: str


@dataclass(frozen=True)
class LaborProductivity:
    labor_hours: Decimal
    rooms_sold: Decimal
    hours_per_occupied_room: Decimal | None
    labor_cost: Decimal | None
    cost_per_occupied_room: Decimal | None
    cost_suppressed: bool


def labor_productivity(
    session: Session, property_id: str, start: date, end: date
) -> LaborProductivity:
    """Labor HOURS and labor COST per occupied room over [start, end].

    Hours per occupied room is PURELY operational — never disclosure-gated, so
    Schedule-15 detail (hours) is always complete. Cost per occupied room is
    per-employee MONEY, so it composes with the SAME per-day `_discloses` guard
    the SOS uses: `_labor_sections` is called PER DAY (never one SUM over the
    caller-controlled window, which would be a differencing oracle — see
    reporting._discloses), and if ANY contributing day's cost is not disclosable
    the whole cost figure is withheld (`labor_cost`/`cost_per_occupied_room` None,
    `cost_suppressed` True). Days are the finest grain a caller can select, so a
    window that withholds nothing is a sum of >= 2-priced-employee days.
    """
    sold = sum(
        _stat_by_day(session, property_id, start, end, "ROOMS_OCCUPIED").values(),
        Decimal("0"),
    )
    hours_total = Decimal("0")
    cost_total = Decimal("0")
    suppressed = False
    for d in _days(start, end):
        # PER DAY: `_labor_sections` returns
        # (lines, cost_total, hours_total, ot_total, fte, suppressed_units,
        #  unpriced_hours). `cost` already EXCLUDES any unit withheld on the day,
        # and `day_suppressed` is the COUNT (>0 == truthy) of units withheld.
        _lines, cost, hours, _ot, _fte, day_suppressed, _unpriced = _labor_sections(
            session, property_id, d, d
        )
        hours_total += hours
        if day_suppressed:
            suppressed = True
        else:
            cost_total += cost
    labor_cost = None if suppressed else cost_total
    return LaborProductivity(
        labor_hours=hours_total,
        rooms_sold=sold,
        hours_per_occupied_room=_ratio(hours_total, sold),
        labor_cost=labor_cost,
        cost_per_occupied_room=None if labor_cost is None else _ratio(labor_cost, sold),
        cost_suppressed=suppressed,
    )


@dataclass(frozen=True)
class ReconLine:
    """One cross-check row: the COMPUTED value (authoritative), the PMS's own
    INGESTED value (the check), and whether they agree within tolerance. `agrees`
    is None when either side is absent — there is nothing to reconcile."""

    computed: Decimal | None
    ingested: Decimal | None
    agrees: bool | None


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _recon_line(
    computed: Decimal | None, ingested: Decimal | None, tol: Decimal
) -> ReconLine:
    if computed is None or ingested is None:
        return ReconLine(computed=computed, ingested=ingested, agrees=None)
    agrees = abs(computed - ingested) <= tol
    return ReconLine(computed=computed, ingested=ingested, agrees=agrees)


_OCC_TOL = Decimal("0.005")  # occupancy is a fraction
_CURRENCY_TOL = Decimal("0.5")  # ADR/RevPAR are currency amounts


def reconciliation(
    session: Session,
    property_id: str,
    start: date,
    end: date,
    metrics: CoreMetrics,
) -> dict[str, ReconLine]:
    """FAIL-SOFT cross-check of the COMPUTED occupancy/ADR/RevPAR (authoritative,
    from `core_metrics`) against the PMS's own INGESTED DAY statistics
    (OCCUPANCY_PCT/ADR/REVPAR). REPORTS a divergence beyond a rounding tolerance;
    NEVER raises — the ingested figures are only a check on our arithmetic.

    Units: ingested OCCUPANCY_PCT is a PERCENTAGE (label "% Rooms Occupied", stored
    verbatim by the adaptor), so it is divided by 100 to match the fraction
    `metrics.occupancy` carries. ADR and REVPAR are currency in both — no scaling.

    Window aggregation: for a multi-day window the ingested side is the MEAN of the
    ingested DAY values over the days where each stat is present (None if none
    present); a single-day window degenerates to that day's stat. This mirrors the
    "statistics are as-of KPIs, never summed" convention and keeps the ingested
    ADR/RevPAR/occupancy on the same per-available-room scale as the computed side.

    Tolerance: occupancy within 0.005 (fraction), ADR/RevPAR within 0.5 (currency).
    `agrees` is None whenever either side is absent — nothing to reconcile.
    """
    occ_pct = _mean(
        list(_stat_by_day(session, property_id, start, end, "OCCUPANCY_PCT").values())
    )
    ingested_occ = None if occ_pct is None else occ_pct / Decimal("100")
    ingested_adr = _mean(
        list(_stat_by_day(session, property_id, start, end, "ADR").values())
    )
    ingested_revpar = _mean(
        list(_stat_by_day(session, property_id, start, end, "REVPAR").values())
    )
    return {
        "occupancy": _recon_line(metrics.occupancy, ingested_occ, _OCC_TOL),
        "adr": _recon_line(metrics.adr, ingested_adr, _CURRENCY_TOL),
        "revpar": _recon_line(metrics.revpar, ingested_revpar, _CURRENCY_TOL),
    }


def core_metrics(
    session: Session, property_id: str, start: date, end: date, *, basis: str
) -> CoreMetrics:
    """Occupancy, ADR, RevPAR, TRevPAR over [start, end]. Denominator is
    inventory.rooms_available (fail-loud). ADR divides room revenue by the
    basis-adjusted rooms-sold; occupancy uses ROOMS_OCCUPIED as-is."""
    avail = Decimal(str(rooms_available(session, property_id, start, end)))
    sold = sum(
        _stat_by_day(session, property_id, start, end, "ROOMS_OCCUPIED").values(),
        Decimal("0"),
    )
    adr_sold = adr_rooms_sold(session, property_id, start, end, basis)
    room_rev = sum(
        _stat_by_day(session, property_id, start, end, "ROOM_REVENUE").values(),
        Decimal("0"),
    )
    total_rev = sum(
        _stat_by_day(session, property_id, start, end, "TOTAL_REVENUE").values(),
        Decimal("0"),
    )
    return CoreMetrics(
        start=start, end=end, rooms_available=avail, rooms_sold=sold, adr_rooms_sold=adr_sold,
        room_revenue=room_rev, total_revenue=total_rev,
        occupancy=_ratio(sold, avail), adr=_ratio(room_rev, adr_sold),
        revpar=_ratio(room_rev, avail), trevpar=_ratio(total_rev, avail),
        adr_room_basis=basis,
    )


_COMPARISON_METRICS: tuple[str, ...] = ("occupancy", "adr", "revpar", "trevpar")
_Q_PCT = Decimal("0.1")


@dataclass(frozen=True)
class Comparison:
    current: CoreMetrics
    prior_period: CoreMetrics | None
    prior_year: CoreMetrics | None
    prior_period_delta_pct: dict[str, Decimal | None]
    prior_year_delta_pct: dict[str, Decimal | None]


def _delta_pct(cur: Decimal | None, prior: Decimal | None) -> Decimal | None:
    """Percentage variance of `cur` over `prior`. None (never a ZeroDivisionError)
    when either operand is absent or the prior base is zero — there is no defined
    variance from nothing or from zero."""
    if cur is None or prior is None or prior == 0:
        return None
    return ((cur - prior) / prior * 100).quantize(_Q_PCT)


def _delta_map(
    current: CoreMetrics, prior: CoreMetrics | None
) -> dict[str, Decimal | None]:
    """Per-metric variance of `current` vs `prior` over the core KPI names. When
    the prior window did not resolve (None) every delta is None."""
    return {
        name: _delta_pct(
            getattr(current, name), None if prior is None else getattr(prior, name)
        )
        for name in _COMPARISON_METRICS
    }


def _shift_year(d: date, years: int) -> date:
    """`d` shifted by whole `years`. A Feb-29 date has no counterpart in a
    non-leap target year (`date.replace` raises ValueError); fall back to a plain
    365-day shift there so a leap-day window degrades gracefully instead of
    blowing up the whole comparison."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d + timedelta(days=365 * years)


def _window_has_history(
    session: Session, property_id: str, start: date, end: date
) -> bool:
    """Whether ANY promoted DAY statistic landed in [start, end]. A prior window
    with no facts is 'less than a year of history' (or no prior period) — there is
    nothing to compare against, so the comparison is None rather than a spurious
    zero-occupancy row invented from an in-force room count alone."""
    return session.execute(
        select(UsaliStatisticFact.property_id)
        .where(
            UsaliStatisticFact.property_id == property_id,
            UsaliStatisticFact.business_date >= start,
            UsaliStatisticFact.business_date <= end,
            UsaliStatisticFact.period == "DAY",
            UsaliStatisticFact.is_prior_year.is_(False),
        )
        .limit(1)
    ).first() is not None


def _prior_core_metrics(
    session: Session, property_id: str, start: date, end: date, *, basis: str
) -> CoreMetrics | None:
    """`core_metrics` for a PRIOR window, degraded to None when the window cannot
    resolve: no landed history (`_window_has_history`), no inventory in force
    (InventoryNotConfigured), or an ADR basis lacking its segment data
    (AdrBasisUnavailable). A prior window that cannot be computed simply yields no
    comparison — never a failed request and never a divide-by-zero downstream."""
    if not _window_has_history(session, property_id, start, end):
        return None
    try:
        return core_metrics(session, property_id, start, end, basis=basis)
    except (InventoryNotConfigured, AdrBasisUnavailable):
        return None


def compare(
    session: Session, property_id: str, start: date, end: date, *, basis: str
) -> Comparison:
    """Current-window `core_metrics` plus a prior-PERIOD comparison (the
    same-length window immediately before [start, end]) and a prior-YEAR
    comparison (the same window one calendar year earlier), each with per-metric
    percentage variance over occupancy/ADR/RevPAR/TRevPAR.

    Prior-period window: the `n = (end - start).days + 1` days ending the day
    before `start`. Prior-year window: `start`/`end` shifted back one year, with
    Feb-29 handled by `_shift_year` (falls back to a 365-day shift in a non-leap
    year rather than raising). Either prior window is None when it cannot resolve
    (no inventory/history, or an unavailable ADR basis) — see `_prior_core_metrics`
    — and its delta map is then all-None. `_delta_pct` never divides by zero: a
    zero prior base yields None, so a property with less than a year of history has
    prior_year None and every prior_year delta None.
    """
    current = core_metrics(session, property_id, start, end, basis=basis)
    n = (end - start).days + 1

    pp_start, pp_end = start - timedelta(days=n), end - timedelta(days=n)
    prior_period = _prior_core_metrics(session, property_id, pp_start, pp_end, basis=basis)

    py_start, py_end = _shift_year(start, -1), _shift_year(end, -1)
    prior_year = _prior_core_metrics(session, property_id, py_start, py_end, basis=basis)

    return Comparison(
        current=current,
        prior_period=prior_period,
        prior_year=prior_year,
        prior_period_delta_pct=_delta_map(current, prior_period),
        prior_year_delta_pct=_delta_map(current, prior_year),
    )


_METRIC_NAMES: tuple[str, ...] = ("occupancy", "adr", "revpar", "trevpar")


@dataclass(frozen=True)
class TrendPair:
    """A current value against a comparison base (WoW average, or the same weekday
    a week prior), with signed percentage variance. Any field is None when its
    base has no complete-day data — a data-incomplete day never fabricates a leg."""

    current: Decimal | None
    prior: Decimal | None
    delta_pct: Decimal | None


@dataclass(frozen=True)
class RollingStat:
    """30-day rolling summary of one metric over the COMPLETE days in the window:
    the average, the population standard deviation (None with fewer than two
    complete values — dispersion is undefined from one point), and `n`, the count
    of complete days that fed it."""

    avg: Decimal | None
    stdev: Decimal | None
    n: int


@dataclass(frozen=True)
class Trends:
    anchor: date
    wow: dict[str, TrendPair]
    mtd: dict[str, Decimal | None]
    rolling_30: dict[str, RollingStat]
    dow: dict[str, TrendPair]


def _daily_series(
    session: Session, property_id: str, start: date, end: date, basis: str
) -> dict[date, dict[str, Decimal | None]]:
    """Per-day core metrics for the COMPLETE days in [start, end] — the days with a
    landed statistics report AND in-force inventory (`complete_days`). A day
    without a landed report is absent from the series entirely, so no trend base
    computed off this series can be moved by it."""
    keep = complete_days(session, property_id, start, end)
    series: dict[date, dict[str, Decimal | None]] = {}
    for d in sorted(keep):
        m = core_metrics(session, property_id, d, d, basis=basis)
        series[d] = {
            "occupancy": m.occupancy,
            "adr": m.adr,
            "revpar": m.revpar,
            "trevpar": m.trevpar,
        }
    return series


def _values_over(
    series: dict[date, dict[str, Decimal | None]], days: list[date], name: str
) -> list[Decimal]:
    """The non-None `name` values for the `days` present in `series`. Absent days
    (data-incomplete, so never seeded into the series) simply do not contribute."""
    out: list[Decimal] = []
    for d in days:
        day = series.get(d)
        if day is None:
            continue
        v = day.get(name)
        if v is not None:
            out.append(v)
    return out


def _mean_over(
    series: dict[date, dict[str, Decimal | None]], days: list[date], name: str
) -> Decimal | None:
    """Mean of the complete-day `name` values over `days`, quantized to 4dp; None
    when no complete day in the range carries the metric."""
    vals = _values_over(series, days, name)
    if not vals:
        return None
    return (sum(vals, Decimal("0")) / Decimal(len(vals))).quantize(_Q4)


def _rolling_stat(
    series: dict[date, dict[str, Decimal | None]], days: list[date], name: str
) -> RollingStat:
    vals = _values_over(series, days, name)
    if not vals:
        return RollingStat(avg=None, stdev=None, n=0)
    avg = (sum(vals, Decimal("0")) / Decimal(len(vals))).quantize(_Q4)
    stdev = None
    if len(vals) >= 2:
        stdev = Decimal(str(statistics.pstdev(float(v) for v in vals))).quantize(_Q4)
    return RollingStat(avg=avg, stdev=stdev, n=len(vals))


def trends(
    session: Session, property_id: str, anchor: date, *, basis: str
) -> Trends:
    """Operator trend bases anchored on `anchor`, EACH computed only over
    data-complete days (`complete_days`): a business date without a landed
    statistics report is excluded from the series and therefore cannot move any
    average.

    - WoW: current == mean of the 7 days ending on `anchor` (anchor-6..anchor);
      prior == mean of the preceding 7 (anchor-13..anchor-7).
    - MTD: mean from the first of `anchor`'s month through `anchor`.
    - rolling_30: average + population stdev + complete-day count over
      anchor-29..anchor.
    - DoW: `anchor`'s value against the same weekday one week earlier (anchor-7).

    All four maps are built from ONE `_daily_series` spanning the widest window
    needed — `min(anchor-29, first-of-month)`..anchor — so the complete-day set is
    resolved once. Percentage variances reuse `_delta_pct` (None on an absent or
    zero base)."""
    first_of_month = anchor.replace(day=1)
    window_start = min(anchor - timedelta(days=29), first_of_month)
    series = _daily_series(session, property_id, window_start, anchor, basis)

    wow_current = [anchor - timedelta(days=i) for i in range(0, 7)]
    wow_prior = [anchor - timedelta(days=i) for i in range(7, 14)]
    mtd_days = _days(first_of_month, anchor)
    rolling_days = _days(anchor - timedelta(days=29), anchor)
    anchor_prior = anchor - timedelta(days=7)

    wow: dict[str, TrendPair] = {}
    mtd: dict[str, Decimal | None] = {}
    rolling_30: dict[str, RollingStat] = {}
    dow: dict[str, TrendPair] = {}
    for name in _METRIC_NAMES:
        cur = _mean_over(series, wow_current, name)
        prior = _mean_over(series, wow_prior, name)
        wow[name] = TrendPair(current=cur, prior=prior, delta_pct=_delta_pct(cur, prior))

        mtd[name] = _mean_over(series, mtd_days, name)

        rolling_30[name] = _rolling_stat(series, rolling_days, name)

        d_cur = series.get(anchor, {}).get(name)
        d_prior = series.get(anchor_prior, {}).get(name)
        dow[name] = TrendPair(
            current=d_cur, prior=d_prior, delta_pct=_delta_pct(d_cur, d_prior)
        )

    return Trends(anchor=anchor, wow=wow, mtd=mtd, rolling_30=rolling_30, dow=dow)
