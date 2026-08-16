# Core performance statistics (revenue + productivity) — design

Issue #9. Compute and expose, for any date range **and** any named fiscal
period, the performance KPIs owners, lenders and management companies read
first — recomputed from primitives with reconciliation cross-checks — plus the
operator trend bases, gated on a real per-day data-completeness signal.

## Scope decision (why this is "revenue + productivity", not the full six)

The operating statement is **revenue-only** today: `summary_operating_statement`
sums USALI Schedules 1–4 and *rejects* expense Schedules 5–16. There is no
gross-operating-profit anywhere, and financial facts carry no department for
non-labor cost. So of the six metrics the issue lists:

- **Buildable now** (this issue): Occupancy, ADR, RevPAR, TRevPAR, plus the two
  productivity statistics — **labor-hours per occupied room** and **labor-cost
  per occupied room** (labor CPOR, total and by department).
- **Blocked on the expense side** (moved to issue #26 — "Expense ingestion &
  P&L", both-sources/QBO-first): **GOPPAR** and **general (non-labor) CPOR**.

This issue ships the numbers that need no expense feed; #26 builds the expense
feed that unblocks the rest. The two are parallel-safe.

## Metrics

All metrics are **recomputed from primitives**, never surfaced from the PMS's
own ingested KPI values — those become reconciliation cross-checks (see below).

| Metric | Formula | Primitive sources |
|---|---|---|
| Occupancy % | rooms sold ÷ rooms available | `ROOMS_OCCUPIED` (daily stat) ÷ `inventory.rooms_available` |
| ADR | room revenue ÷ rooms sold *(ADR basis)* | `ROOM_REVENUE` ÷ (rooms sold − comp/house per treatment) |
| RevPAR | room revenue ÷ rooms available | direct **and** ADR × occupancy; the two must agree (AC) |
| TRevPAR | total revenue ÷ rooms available | `TOTAL_REVENUE` (daily stat) |
| Labor hours / occupied room | Σ `UsaliLaborFact.hours` ÷ rooms sold | pure hours — **not** disclosure-gated |
| Labor cost / occupied room (labor CPOR) | Σ labor cost ÷ rooms sold, **total + by department** | disclosure-gated (per-employee money) |

Rooms sold per property per day comes from the existing `_rooms_by_day`
accessor (`UsaliStatisticFact`, `metric_code == "ROOMS_OCCUPIED"`, `period ==
"DAY"`, `is_prior_year == False`). Room revenue and total revenue come from the
`ROOM_REVENUE` / `TOTAL_REVENUE` daily statistics; room revenue is
cross-checked against the financial-fact `usali_sub_category == "Rooms"` sum.

### Reconciliation cross-checks (fail soft, always reported)

The platform ingests the PMS's own `ADR`, `REVPAR`, `OCCUPANCY_PCT` and
`ROOMS_AVAILABLE` statistics. Each computed metric is compared to its ingested
counterpart; a divergence beyond a rounding tolerance is **reported in the
response** (a `reconciliation` block), not raised — the computed value is
authoritative, the ingested value is a check. The one hard assertion is the AC's
own: RevPAR-direct and ADR × occupancy must agree to within tolerance (a test
pins this on seed data).

## Denominator and DNR

`inventory.rooms_available` (issue #8; fail-loud on an unconfigured day or on
out-of-service rooms exceeding inventory) is **authoritative**. The ingested
`ROOMS_AVAILABLE` statistic is a reconciliation cross-check only.

**Do-Not-Rent (DNR).** Rooms intentionally held out of sale (owner units, model
rooms, long-term holds) are identical *in effect* to out-of-order rooms — they
reduce sellable inventory — but differ in reason. Rather than a new concept,
extend the out-of-service reason vocabulary (`OOO_REASON_CODES` + the DB CHECK)
with `do_not_rent` and `owner_occupied`, so DNR rooms flow through
`rooms_available` and every occupancy denominator automatically. This is a
one-line vocabulary extension plus a migration that widens the CHECK; the
CHECK↔frozenset coupling test added in the #8 review keeps them in sync.

DNR is a distinct axis from comp/house-use: DNR rooms are **not sold and not
available**; comp/house-use rooms are **occupied** (in the numerator) but
excluded from ADR's rooms-sold basis.

## Comp / house-use treatment (ADR basis)

The AC requires comp and house-use rooms excluded from rooms-sold for ADR,
**configurable per property, with the treatment stated in the response**.
Comp/house-use is tracked only at the market-segment grain (`UsaliSegmentFact`,
segment kinds `COMPLIMENTARY` / `HOUSE_USE`) and is not reconciled to the
headline `ROOMS_OCCUPIED` statistic.

- New per-property config field **`adr_room_basis ∈ {as_reported,
  exclude_comp_house}`**, added to the property-config surface (issue #8's
  `fiscal_calendar`/property config family). Default `as_reported`.
- When `exclude_comp_house`, ADR's rooms-sold = `ROOMS_OCCUPIED` −
  Σ(segment rooms where kind ∈ {COMPLIMENTARY, HOUSE_USE}) for the window.
- Every metric response states the **treatment applied** and **whether segment
  data was available** for the window.
- Fail-loud: `exclude_comp_house` with no segment data for a day in the window
  refuses (adr-010) rather than silently applying no exclusion. `as_reported`
  never needs segment data.

Occupancy % uses `ROOMS_OCCUPIED` as-is for the numerator (comp/house rooms are
genuinely occupied); only ADR's *rooms-sold basis* nets them out. This split is
stated explicitly in the response and the docs.

## Comparisons and trend bases

**Comparisons** (per metric, point value + percentage variance):

- **Prior-period**: the same-length window immediately before the requested one.
- **Prior-year**: the same window shifted back one year (or the corresponding
  fiscal period of the prior fiscal year, when the request is period-based).
- A property with **less than a full year of history** yields `null`/`n/a` for
  the prior-year comparison — never a divide-by-zero (AC).

**Trend bases** (daily series, all excluding data-incomplete days):

- **WoW** — current-7-day average vs prior-7-day average.
- **MTD** — month-to-date average (alongside the existing MoM the SOS already
  carries).
- **30-day rolling** average **and standard deviation** per metric.
- **Day-of-week** — a metric vs the same weekday one week prior (this Saturday
  vs last Saturday).

## Data completeness — the ingestion-coverage table

Trend bases and comparisons must exclude data-incomplete days. No per-day
completeness signal exists today (issue #19 owns the richer version). This issue
builds the foundational table:

- New **OrgScoped** table **`ingestion_coverage`**, keyed
  `(org_id, property_id, business_date, report_type)`, recording which source
  report types landed for each property-day. Written by the ingestion pipeline
  as files are processed (one row per landed report type per day).
- A day is **metric-complete** when the required report types for these metrics
  are present: the PMS statistics report (supplying `ROOMS_OCCUPIED`,
  `ROOM_REVENUE`, `TOTAL_REVENUE`) — and `rooms_available` resolves for the day.
- Trends and comparisons consume only metric-complete days; the response reports
  how many days in the window were excluded as incomplete.
- The same table gives visibility into the **#26 expense surface** coming
  online (a new expense `report_type` will appear here as it lands), which is the
  "track the new ingestion surface" requirement.

`ingestion_coverage` joins the L2 tenancy wall on the same terms as every other
org-scoped table (composite `(org_id, property_id)` FK to `property`, RLS
ENABLE+FORCE + the verbatim `org_wall` policy, org_id server-default + index),
and updates the four sync-registry tests (`test_l1`, `test_l2`, `test_l4` head
pin, `test_models`) that every new table must.

## Disclosure and drill-through

**Disclosure.** Room and revenue metrics carry no per-employee money, so they
are ungated. Labor-**hours** per occupied room is a pure-hours metric —
ungated. Labor-**cost** per occupied room (total and by department) carries
per-employee money and is a differencing-oracle surface over a caller-controlled
window; it is computed through the existing `_labor_sections` + `_discloses`
guard **per business day** (reusing the `labor_analytics` pattern), never a
fresh `SUM` — so a per-day gate holds and two windows cannot be subtracted to
isolate one person's pay. A day whose labor cost is under-populated
(< 2 priced employees) suppresses the cost figure for that day, exactly as the
SOS does.

**Drill-through.** Each metric drills through to underlying staged transactions
consistent with the operating statement: revenue-derived numerators (room
revenue, total revenue) drill through the financial-fact → `stage_id` join
(`line_transactions`); occupancy/ADR's statistic inputs drill through the
statistic-stage (`UsaliStatisticFact.stat_stage_id` → `PmsDailyStatisticStage`);
labor drills through to `timecard_id`. The response documents the drill-through
target per metric.

## API surface

Pure functions in `reporting.py` (alongside `labor_analytics`, `_rooms_by_day`,
`_revenue_by_day`), returning frozen dataclasses; HTTP in `portal_api.py` under
the existing `/api` router, gated by `require_property_access`:

- `GET /api/performance?property=&from=&to=` — the metric set with comparisons,
  trend bases, stated treatments, reconciliation and completeness info, for a
  date range.
- `GET /api/performance?property=&period=YYYY-Pnn` — the same for a named fiscal
  period (resolved via `fiscal.resolve_period`).

The per-property `adr_room_basis` setting is written through the existing
property-config write surface (issue #8), org_admin | property_gm, audited.

## Frontend

A KPI/performance dashboard page: occupancy/ADR/RevPAR/TRevPAR cards with
prior-period and prior-year deltas, the labor-productivity statistics, and trend
sparklines (WoW / 30-day rolling / day-of-week). The stated comp/house-use
treatment and any reconciliation divergences are surfaced. Property comes from
the global top-bar selector (same as the other pages).

The **weekly narrative recap** (prose commentary in the Labor-Flash style) is
**not** part of this issue — it is its own issue (#14, weekly narrative), which
consumes these metrics.

## Testing / acceptance

- Each metric available for a date range **and** a named fiscal period.
- RevPAR-direct and ADR × occupancy agree within a rounding tolerance — a test
  asserts this on seed data.
- Comp/house-use excluded from ADR's rooms-sold when the property treatment says
  so, included otherwise; treatment stated in the response; `exclude_comp_house`
  with no segment data refuses.
- Every metric drills through to staged transactions, reconciling to the metric.
- Prior-year comparison on a property with < 1 year of history returns n/a, no
  divide-by-zero.
- Trend bases exclude data-incomplete days; a half-ingested day is excluded and
  the exclusion count reported.
- Labor-cost-per-occupied-room composes with `_discloses` per day: a
  differencing-oracle test (two subtractable windows) proves one person's cost
  cannot be isolated; labor-hours-per-occupied-room is never suppressed.
- `ingestion_coverage` migration round-trips, joins the RLS wall, and updates
  the four sync-registry tests.
- Formulas documented in `docs/reference/performance-metrics.md` (extending the
  #8 foundations doc).

## Out of scope

- GOPPAR, general (non-labor) CPOR, GOP, any expense-side metric → issue #26.
- Comp-set / STAR indexing, forecasting (the issue's own out-of-scope).
- The weekly narrative recap → issue #14.
- The richer data-completeness model (expected-vs-landed per property) → issue
  #19; this issue builds only the coverage table it needs.

## Dependencies

- Issue #8 (rooms-available denominator, fiscal periods, property-config
  surface). The rooms-available fail-loud contract used here includes the
  `InventoryInconsistent` refusal added in the #8 three-lens review remediation
  (PR #25) — that should land before this issue's metric code.
- Parallel-safe with issue #26 (expense ingestion).
