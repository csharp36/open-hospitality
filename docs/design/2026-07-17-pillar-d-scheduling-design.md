# Pillar D — Scheduling & Labor Planning Design

**Date:** 2026-07-17
**Status:** Approved for planning
**Depends on:** Pillar A (identity, scoped RBAC), Pillar B (kiosk capture, timecards, the California overtime engine, estimated labor cost), Pillar C (actual labor cost, variance) — all merged.

## Context: the loop closes forward

Pillar B locked scheduling's driver when it deferred it: **labor-cost/overtime control — planning hours
against a target** — and noted you cannot forecast labor without actuals. That prerequisite is now met.
The platform has actual hours (B), estimated and actual labor cost (B3/C), a pure California overtime
engine (`usali.overtime`), and daily occupancy/revenue facts from the PMS. Pillar D completes the
management loop: **plan (D) → capture (B) → estimate (B3) → actual (C2) → variance (C3)** — the OT that
today appears on a variance report two weeks late becomes visible on a draft schedule before the week
starts.

One constraint shapes everything: the scheduled population is **passwordless** (the Pillar B premise).
Employees cannot log in to see a schedule; whatever we build must reach them another way.

## Goal

A GM assembles next week's schedule from shift templates and sees, live as they assign, the projected
regular/OT/double-time hours per employee and the projected cost per department — with warnings before
the week starts. Publishing locks and versions the schedule, prints a wall grid, and (D2) surfaces each
employee's week on the kiosk they already punch on. Targets derived from labor standards × a GM-entered
occupancy forecast make the schedule answerable to demand; adherence reporting (D3) closes plan vs
reality.

## Decisions locked with the user (2026-07-17)

1. **The first shippable payoff is the OT-aware schedule builder** — not occupancy targets, not a bare
   digital rota. The existing CA overtime engine prices a draft schedule exactly as it prices a
   timecard, so scheduled OT and department cost are known and controllable in advance. Targets come
   second (D2).
2. **Publishing = kiosk my-week + wall print.** The enrolled kiosk is the schedule's front door: an
   employee taps their name (the same flow as punching) and sees their own shifts. Plus a printable
   week grid for the back office. No new auth surface, no new infrastructure. SMS is a later add-on
   (new provider, phone-number PII, consent handling), not a v1 requirement.
3. **The occupancy forecast is GM-entered, history-hinted.** The GM types expected occupied rooms per
   day (they already know it from their PMS's on-the-books view); the form shows same-day-last-week and
   trailing-average hints computed from our own promoted facts beside the input. Honest about what we
   have: our ingested reports are dailies, not future bookings — the hint informs, never dictates.

## Two load-bearing consistency rules

1. **Schedule week = the payroll workweek.** Schedule weeks start on the same Monday grid as the
   biweekly payroll periods (`payroll_period_anchor`; a schedule week is exactly half a pay period).
   Weekly-40 and 7th-consecutive-day projection are therefore **exact within a single schedule week** —
   no cross-week reasoning, and the same `period_for`-style arithmetic used everywhere else.
2. **The PII discipline carries over unchanged.** A GM must never learn an individual pay rate — and a
   per-employee projected *cost* would hand them `rate = cost ÷ hours`. The builder therefore shows
   per-employee projected **hours** (which is what OT warnings need: "this assignment puts X into 4h of
   overtime") but projected **cost only as department and schedule aggregates**, with B3's
   fewer-than-two-distinct-employees suppression applied to those aggregates. Warnings speak in hours,
   never money. Rates are read server-side during projection and never leave it.

## Architecture

```
shift_template (per department, property-local start/end times)
    → GM assembles a week (DRAFT): assigns employees to shifts; open shifts allowed
    → LIVE projection on every edit:
         draft shifts → usali.overtime.compute_overtime → projected reg/OT/DT per employee
         + pay rates read server-side → projected cost as DEPARTMENT AGGREGATES only
    → warnings (flag, never block): scheduled weekly-40 OT; >8h scheduled days;
         clopening (rest between consecutive shifts < a configurable floor, default 10h);
         7th consecutive scheduled day
      hard 422s (data errors, not judgment calls): overlapping shifts for one employee;
         assigning a terminated employee
    → publish → locked + audited; a later edit republishes as version+1
    → wall-print week grid  +  kiosk "my week" (D2)
    → (D2) targets: labor_standard × occupancy_forecast vs scheduled, per department per day
    → (D3) adherence: scheduled vs punched, after the fact
```

**Warnings flag, never block**, because a 9-hour scheduled day may be deliberate (a banquet, a deep
clean). The GM decides; the system makes the cost of the decision visible. The two 422s are data
errors, not judgment calls.

**Open shifts** (`employee_id` NULL) represent planned-but-unassigned coverage. They count toward a
department's scheduled hours and (D2) target coverage, but toward no one's OT and no cost projection —
an open shift has no rate.

## Data model (Alembic migrations)

- **`shift_template`** — `(template_id, property_id, department_id, name, start_time, end_time,
  crosses_midnight)`. Times are property-local (the property's IANA timezone from B1); a
  crosses-midnight shift ends the next calendar day.
- **`schedule`** — `(schedule_id, property_id, week_start, status, version, published_by,
  published_at)`. `week_start` is a Monday on the payroll grid (validated:
  `(week_start − payroll_period_anchor) % 7 == 0` and weekday == Monday). `status: draft → published`.
  Unique `(property_id, week_start)`; republishing bumps `version` and re-stamps, with an audit event.
- **`shift`** — `(shift_id, schedule_id, business_date, department_id, start_time, end_time,
  crosses_midnight, employee_id NULLABLE, template_id NULLABLE)`. `employee_id` NULL = open shift.
  `template_id` records provenance but shifts are free-standing once created (editing a template never
  rewrites existing shifts).
- **(D2) `labor_standard`** — `(standard_id, property_id, department_id, basis, value)` where
  `basis ∈ {fixed_hours_per_day, minutes_per_occupied_room}`.
- **(D2) `occupancy_forecast`** — `(forecast_id, property_id, business_date, occupied_rooms,
  entered_by, entered_at)`. GM-entered; the UI shows history hints beside the input.
- **(D3)** no new tables — adherence joins `shift` to `punch` by employee/date.

## RBAC (reuses existing concepts — nothing new)

- **Build / edit / publish:** `org_admin` or `property_gm`, property-confined via `assignment_scope`
  (the timecard-approval pattern — a co-held global VIEW role grants nothing).
- **Kiosk my-week (D2):** the enrolled device token (B1's `X-Kiosk-Token`), property-scoped. An
  employee taps their name and sees only their own shifts from the latest **published** version. Draft
  schedules are never visible on the kiosk.
- **Print:** any operator role for their scope (it is the wall poster).
- **Projected cost:** department/schedule aggregates only, B3 suppression applied. No route returns
  per-employee money — projected or otherwise — outside Payroll-Admin surfaces that already exist.

## Projection semantics (D1)

- Projection runs over the **draft schedule week only**: scheduled shift durations per employee per
  business date → `compute_overtime(day_hours, anchor, exempt)` → reg/OT/DT. Exempt employees project
  hours with no OT (the engine's existing rule) and **no cost** (the B3 rule: salaried staff are never
  hourly-costed).
- Lunches: a shift longer than a configurable threshold (default 6h) assumes an unpaid 30-minute meal
  deduction in projected hours, mirroring how a real day at that length will be punched. Stated on the
  page footer.
- Employees with no pay rate project hours normally and cost 0, surfaced as the existing
  unpriced-hours note — consistent with B3.
- Current-week projection (merging punches-to-date with remaining shifts) is **D3**, not D1 — D1
  projects future weeks from scheduled hours only.

## Key flows

- **Template setup:** GM defines the property's recurring shifts once (e.g. Front Desk AM 7–15, PM
  15–23, Audit 23–7 crosses-midnight, Housekeeping 9–17:30).
- **Build a week:** pick the week (Monday grid) → the grid shows days × departments → add shifts from
  templates → assign employees (or leave open). Every edit re-projects; warnings appear inline on the
  employee and day they concern.
- **Publish:** locks the version, writes the audit event, makes it printable and (D2) kiosk-visible.
  Editing after publish is allowed but produces version+1 on republish — the wall copy and kiosk always
  show the latest published version, and the version history is the paper trail for "the schedule
  changed after I saw it."
- **(D2) Targets:** GM enters occupied rooms per day (hints shown); target hours per department per day
  = standards applied to the forecast; the builder shows target vs scheduled hours/cost per department,
  colored by over/under.
- **(D3) Adherence:** after the week, scheduled vs punched hours per department/day plus an exceptions
  list (no-show: scheduled but never punched; unscheduled punch; large deviation). Hours only at the
  employee level — money stays aggregate.

## Security & PII

- No new PII classes. No per-employee money on any new surface (rule 2 above). Schedule data itself
  (who works when) is operationally sensitive but not statutory PII; kiosk exposure is deliberately
  limited to the tapping employee's own shifts.
- Publishing/republishing and standard/forecast changes write audit events (who changed the plan, when).
- The kiosk my-week endpoint is device-token-authenticated and property-scoped exactly like punching;
  it returns only the tapped employee's shifts, never the whole grid.

## Testing & gates

- Pure projection tests reuse the overtime engine's test discipline: scheduled-OT boundaries, clopening
  detection across midnight-crossing shifts, 7th-day, exempt, meal-deduction thresholds.
- Property confinement on every route (the A2.3 adversarial pattern); kiosk my-week returns only the
  tapped employee's shifts; draft schedules invisible to the kiosk.
- Cost aggregation: suppression under two distinct employees; no per-employee money in any response
  (the C2/C3 response-text assertion pattern).
- Publish/version semantics: republish bumps version + audits; print and kiosk track the latest
  published version.
- Existing gates unchanged and green throughout: pytest + Testcontainers, strict mypy, ruff; frontend
  build (`tsc -b`), oxlint, vitest, Playwright. The B/C suites must stay green.

## Implementation phases (one spec, planned as three)

- **D1 — The OT-aware builder:** `shift_template`/`schedule`/`shift`, the week builder, live
  projection + warnings, publish/version/audit, the wall print. *Ships: next week's schedule with its
  OT and department-level cost known in advance.*
- **D2 — Targets + the kiosk window:** kiosk my-week; `occupancy_forecast` (GM-entered,
  history-hinted); `labor_standard`; target-vs-scheduled hours/cost per department per day;
  GM-maintained availability notes on employees (the "can't work Tuesdays" aid — deferred from D1 to
  keep it tight). *Ships: the schedule answerable to demand — the second half of the locked driver.*
- **D3 — Adherence:** scheduled-vs-punched per department/day, the exceptions list, and current-week
  projection merging punches-to-date. *Ships: the plan-vs-reality close-out.*

## Out of scope (explicit)

- **Shift swaps/trades and availability requests** — the passwordless population has no input channel;
  GM-maintained availability notes (D2) are the honest substitute until an employee channel exists.
- **Auto-scheduling / optimization** — the GM decides; the system prices the decision.
- **SMS/push notifications** — a later add-on: provider integration, phone-number PII, consent (TCPA).
- **Bookings-based demand forecasting** — our ingested reports are dailies; the GM's number is the
  honest forecast source, hints notwithstanding.
- **Predictive-scheduling ordinance automation** — SF/Emeryville fair-workweek rules do not bind the
  San José pilot; the clopening/rest warnings are a floor, not a compliance claim, and must not be
  presented as one.

## Definition of done

A GM defines shift templates, assembles next week on the Monday grid, and watches projected OT and
department cost update live as they assign — with warnings for scheduled overtime, clopening, and 7th
consecutive days, and hard errors only for true conflicts. Publishing locks a version, audits it, and
produces the wall print; republishing after changes bumps the version. (D2) An employee taps their name
on the kiosk and sees their week; the GM enters occupancy with history hints and sees target vs
scheduled per department. (D3) After the week runs, adherence shows plan vs reality. No route ever
returns per-employee money; all gates green.
