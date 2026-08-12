# Performance metrics — foundations (rooms available & fiscal periods)

Issue #8 establishes the two denominators every performance statistic needs.

## Rooms available

For an inclusive date window `[start, end]` at a property:

    rooms_available = Σ_day∈window ( total_rooms in force that day )
                    − Σ_block ( overlap_nights(block, window) × block.room_count )

- The in-force room count for a day is the `room_inventory` row with the
  greatest `effective_date ≤ day`. Counts are effective-dated and append-only;
  history is never rewritten.
- Each out-of-order block is clamped to the window before counting nights.
- If any day in the window has no in-force inventory row, the computation
  refuses (`InventoryNotConfigured`) rather than assuming a count.

Implemented in `src/usali/inventory.py` (`rooms_available`, `total_rooms_on`).

## Fiscal periods

Each property has one `fiscal_calendar` row. Period keys are `"{fiscal_year}-Pnn"`,
where `fiscal_year` is the calendar year the fiscal year starts in and `nn` is
`01`–`12` (both calendar types have twelve periods).

- **calendar_month:** period *N* is the *N*-th calendar month from
  `fiscal_year_start_month`.
- **4-4-5:** the fiscal year is anchored on the first `week_start_weekday` on or
  after the 1st of the start month; periods are 4/4/5-week blocks per quarter. A
  53-week fiscal year is handled by the final period absorbing the extra week, so
  the calendar always tiles up to the next year's anchor.

Implemented in `src/usali/fiscal.py` (`resolve_period`, `period_containing`,
`periods_in_year`).

# Performance metrics (#9)

Issue #9 recomputes the core operating statistics from primitives over a date
range or a fiscal period, adds prior-period/prior-year comparisons and operator
trend bases, and cross-checks the result against the PMS's own ingested KPIs.

Implemented in `src/usali/performance.py`. Room/revenue metrics carry no
per-employee money and are ungated; labor-cost metrics compose with the same
per-day disclosure guard the SOS uses, so a caller-controlled window cannot be a
differencing oracle. All denominators come from `inventory.rooms_available`
(fail-loud). GOPPAR and general (non-labor) CPOR wait on expense ingestion (#26);
the weekly narrative recap waits on #14.

## Core metrics and their formulas

Over an inclusive window `[start, end]` at a property (`core_metrics`):

    occupancy = rooms_sold ÷ rooms_available
    ADR       = room_revenue ÷ adr_rooms_sold
    RevPAR    = room_revenue ÷ rooms_available
    TRevPAR   = total_revenue ÷ rooms_available

- **rooms_sold** = Σ_day `ROOMS_OCCUPIED` (the promoted DAY statistic per business
  date; statistics are as-of KPIs, summed across days, never re-summed within a
  day).
- **rooms_available** is the #8 denominator (`inventory.rooms_available`,
  fail-loud). `do_not_rent` / `owner_occupied` (DNR) rooms reduce it exactly like
  out-of-order (OOO) rooms.
- **room_revenue** = Σ_day `ROOM_REVENUE`; **total_revenue** = Σ_day `TOTAL_REVENUE`.
- **adr_rooms_sold** is the ADR-basis rooms-sold (comp/house-use netting below).
- Every ratio is quantized to 4 dp; a zero denominator yields `None` (never a
  divide-by-zero), so occupancy/ADR/RevPAR/TRevPAR are `None` when their
  denominator is zero.
- RevPAR is cross-checked against ADR × occupancy in reconciliation and by
  construction agrees within rounding tolerance.

## ADR comp/house-use basis

ADR divides room revenue by rooms sold on the property's configured basis
(`adr_room_basis`), stated back in the response as `adr_room_basis`
(`adr_rooms_sold`):

- **`as_reported`** — `adr_rooms_sold` = Σ `ROOMS_OCCUPIED` (comp/house-use rooms
  included).
- **`exclude_comp_house`** — subtracts the market-segment `COMPLIMENTARY` +
  `HOUSE_USE` rooms per day from the occupied total. If any day with occupied
  rooms has no segment data to net from, it refuses (`AdrBasisUnavailable`, 409)
  rather than silently not-excluding.

Occupancy, RevPAR and TRevPAR always use rooms sold / rooms available as-is; only
ADR's denominator moves with the basis.

## Labor productivity

Per occupied room over the window (`labor_productivity`):

    labor_hours_per_occupied_room = Σ labor_hours ÷ rooms_sold
    labor_cost_per_occupied_room  = Σ labor_cost  ÷ rooms_sold

- **Hours per occupied room** is purely operational and is **never** disclosure-
  gated — Schedule-15 hours are always complete.
- **Cost per occupied room** is per-employee money, so it is computed **per day**
  through the same `reporting._discloses` guard the SOS uses (never one SUM over
  the caller-controlled window). A day funded by fewer than two priced employees
  suppresses that day's cost; if any contributing day is suppressed the whole cost
  figure is withheld (`labor_cost` / `cost_per_occupied_room` `None`,
  `cost_suppressed` True). Hours are never suppressed. A window that withholds
  nothing is therefore a sum of days each funded by ≥ 2 priced employees.

## Reconciliation (fail-soft cross-checks)

`reconciliation` checks the computed occupancy/ADR/RevPAR (authoritative) against
the PMS's own ingested DAY statistics — `OCCUPANCY_PCT`, `ADR`, `REVPAR`. It
**reports** a divergence beyond tolerance and **never raises**; the ingested
figures are only a check on our arithmetic.

- Ingested `OCCUPANCY_PCT` is a percentage (stored verbatim), normalized ÷ 100 to
  match the computed fraction. ADR/RevPAR are currency on both sides — no scaling.
- Multi-day windows take the **mean** of the ingested DAY values over the days
  each stat is present (as-of KPIs, never summed), keeping the ingested side on
  the same per-available-room scale as the computed side.
- Tolerances: occupancy within 0.005 (fraction), ADR/RevPAR within 0.5
  (currency). `agrees` is `None` whenever either side is absent — nothing to
  reconcile.

## Comparisons

`compare` returns the current window plus two prior windows, each with per-metric
point values and percentage variance over occupancy/ADR/RevPAR/TRevPAR:

- **Prior period** — the same-length window immediately before `[start, end]`
  (the `n = (end − start).days + 1` days ending the day before `start`).
- **Prior year** — `start`/`end` shifted back one calendar year; a Feb-29 window
  with no counterpart in a non-leap year degrades to a 365-day shift rather than
  raising (leap-day safe).

A prior window that cannot resolve — less than a year of history / no landed
facts, no in-force inventory, or an unavailable ADR basis — is `None`, and its
delta map is all-`None`. Variance never divides by zero: a zero or absent prior
base yields `None`.

## Trend bases

`trends` anchors on the window end and computes four operator bases, **each over
only data-complete days** (see below) — a business date without a landed
statistics report is excluded from the series and cannot move any average:

- **WoW** — mean of the 7 days ending on the anchor vs the mean of the preceding
  7, with signed % variance.
- **MTD** — mean from the first of the anchor's month through the anchor.
- **Rolling-30** — average, population standard deviation, and the complete-day
  count `n` over the anchor and the 29 days before it. Stdev is `None` with fewer
  than two complete values (dispersion undefined from one point).
- **Day-of-week (DoW)** — the anchor's value against the same weekday one week
  earlier (anchor − 7), with % variance.

## Data completeness

A business date is **metric-complete** when both hold (`complete_days`):

1. a statistics report landed for it — `manager_flash` (OPERA) or
   `manager_report` (AUTOCLERK); a property uses one PMS, so either satisfies it
   (tracked in the `ingestion_coverage` table), and
2. `rooms_available` resolves (an in-force room count exists that day).

Trend bases and the per-day series consume only complete days. The
`GET /api/performance` response reports `days_excluded` = window length minus its
complete-day count.

## Drill-through

Each metric traces to its own source stage:

- **Revenue (room_revenue)** → financial-fact stage, via the room-revenue
  drill-through endpoint (the SOS Rooms revenue line, `line_transactions`), so
  every dollar traces to a staged PMS transaction.
- **Occupancy / ADR** → statistic-fact stage (`ROOMS_OCCUPIED`, revenue, segment
  facts).
- **Labor** → timecards.

## API

`GET /api/performance` (`portal_api.get_performance`) returns the current-window
metrics, prior-period + prior-year comparisons, the reconciliation, the trend
bases, labor productivity, and `days_excluded`. The window is given by **exactly
one** of:

    GET /api/performance?property=&from=YYYY-MM-DD&to=YYYY-MM-DD
    GET /api/performance?property=&period=YYYY-Pnn

`period` is resolved via the property's fiscal calendar and echoed back (`null`
for an explicit range). Passing both forms, neither, or a half-given range is
422. A fail-loud service condition (inventory not configured, ADR basis missing
its segment data, or `period` on a property with no fiscal calendar) is 409; a
malformed or out-of-range `period` key is 422.

The room-revenue drill-through is
`GET /api/performance/room-revenue/transactions?property=&from=&to=`.

## Deferrals

- **GOPPAR** and general (non-labor) **CPOR** → issue #26 (expense ingestion).
- **Weekly narrative recap** → issue #14.
