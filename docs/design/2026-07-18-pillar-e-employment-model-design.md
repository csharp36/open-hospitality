# Pillar E — Employment Model: Assignments, Effective-Dated Rates, Benefits

**Date:** 2026-07-18
**Status:** Design, pending approval

## Goal

Replace the single-property, single-rate `Employee` record with the employment
model the business actually has: people who work at more than one property, hold
more than one job, are paid differently for each, and whose rates change over
time.

## Why now

Field inventory taken from the incumbent system (Inn-Flow, tenant 464) on
2026-07-18 — see `/Volumes/Employees/inn-flow-schema-inventory-2026-07-18.json`,
which is deliberately NOT in this repo because it derives from real employee
records.

Against the real roster of El Sendero LLC's two properties:

- **21 of 28 active employees work at both hotels.** `Employee.property_id` is a
  single FK, so three quarters of the workforce cannot be represented.
- **Three employees hold two different jobs** (`FRONT DESK ASSOCIATE` +
  `NIGHT AUDITOR`; `HOUSEKEEPER` + `LAUNDRY ATTENDANT`; `BREAKFAST ATTENDANT` +
  `HOUSEKEEPER`). `Employee.pay_rate` is one scalar.
- **The incumbent stores four rate types per position** (`Regular`, `OT`, `DOT`,
  `Holiday`) and has a `Special Rates` table with an `Effective` column. We have
  no rate history at all, so re-running a closed period after a raise silently
  re-costs old hours at the new rate — a restated Schedule 14 would not tie to
  what was filed.

These are not future requirements. They mean numbers we already ship are wrong.

## Two findings that shape the design

### 1. Property attribution already exists in the data

`Punch.kiosk_device_id → KioskDevice.property_id`. The physical time clock at
each hotel already records where a person clocked in; nothing reads it.
`Shift.department_id → Department.property_id` gives the same for planned hours.

So we are not adding attribution — we are consuming attribution that has been
recorded since B1 and thrown away ever since.

### 2. Overtime must NOT be computed per property

El Sendero LLC is the employer of record for both hotels. California daily
(>8, >12) and weekly (>40) overtime is computed **per employer**, not per work
location.

> **Citation gate — RESOLVED 2026-07-18.** Grounded in
> [docs/reference/overtime-jurisdictions.md](../reference/overtime-jurisdictions.md).
>
> The mechanism is definitional rather than a special rule. **Cal. Labor Code
> §500** defines a workday as "any consecutive 24-hour period commencing at the
> same time each calendar day" — with no work-site qualifier; it is temporal and
> attaches to the employee-employer relationship. **§510(a)** applies the 8/12
> thresholds to "one workday". **Wage Order 5 (public housekeeping), 8 CCR
> §11050 §2** scopes both "employer" and "hours worked" to the employer, not the
> site. A single LLC is one "person" under Labor Code §18, so 6h + 5h = 11h and
> 3 hours are daily overtime. **29 CFR §778.103** independently requires totalling
> "all the hours worked … even though two or more unrelated job assignments may
> have been performed."
>
> No contrary authority was found. Note two honest gaps: no DLSE opinion letter
> captioned directly on multi-establishment aggregation exists, and the closest
> one (1991.11.01-4) is a scanned PDF whose contents were not verbatim-verified.
>
> **Open question for the business, not the code:** this holds because ONE LLC
> employs both hotels' staff. If SJCES and 58033 are held by separate entities,
> it becomes a joint-employer analysis (DOL Op. Ltr. FLSA-2025-05 aggregated
> hours across nominally separate hotel entities; *Martinez v. Combs* is the
> California counterpart). Aggregation remains the right behaviour either way,
> so the design does not change — but the reasoning does. Confirm the entity
> structure.

The obvious design — one timecard per employee per property — is therefore a
wage-and-hour violation. An employee working 6h at SJCES and 5h at 58033 on one
business date has worked 11 hours and is owed 3 hours of daily OT; per-property
cards would show 6 and 5 and compute none.

**Consequence:** `Timecard`'s existing `UniqueConstraint(employee_id,
period_start)` is already correct and does not change. Hours and OT are computed
on the combined population of punches; only the resulting **cost** is allocated
per property. Allocation happens after the overtime engine, never before.

## Model

```
employee                       identity only
  │                            property_id / department_id / position_id / pay_rate RETIRED
  └── employee_assignment      (employee_id, property_id, department_id, position_id,
        │                       is_primary, status, effective_from, effective_to)
        │
        ├── assignment_rate    (assignment_id, rate_type, amount ENCRYPTED,
        │                       effective_from, effective_to)
        │                       rate_type ∈ regular | ot | dot | holiday
        │
        └── assignment_tax     (assignment_id, unemployment_state, working_state,
                                filing_status, allowances, additional_withholding)
```

`assignment_tax` mirrors the incumbent, where tax elections are nested inside the
per-entity assignment rather than on the employee.

Costing becomes: **punch → device → property → assignment → rate in effect on
that business date.** Every hour prices at the rate that was in force on the day
it was worked, at the property where it was worked.

## Phases

Each phase ships independently and leaves `main` green.

### E1 — Assignment model and property allocation

The structural core. Introduces `employee_assignment`, backfills it from the
current `Employee` columns, and rewires the costing path to allocate hours by
property via the kiosk device. Retires `Employee.property_id`,
`department_id`, `position_id`.

Touches `auth.py` (`resolve_scope` now resolves through assignments),
`workforce.py`, `onboarding.py`, `labor.py`, `payroll_run.py`, `reporting.py`,
`kiosk.py`, `schedule_*.py`, `roster_seed.py`.

**Suppression must be re-derived, not assumed.** The priced population is now
per-assignment rather than per-employee, which changes what sources every
department aggregate. This is the same class as the three Criticals already
found (B3/C3/D1) and the D3 differencing oracle. Treat re-verification as part
of E1's definition of done, with fresh leak tests written against the new shape.

### E2 — Effective-dated per-position rates

Introduces `assignment_rate` with the four rate types and effective dating.
Rate resolution is by business date. Cuts over the four `pay_rate` consumers
(`workforce.py`, `labor.py`, `payroll_run.py`, `schedule_api.py`) and retires
`Employee.pay_rate`.

**Non-negotiable regression:** the cost of a closed period must be
byte-identical regardless of when it is computed. This is the D3 invariant
generalised — previously cost could not vary with `as_of`; now it must not vary
with *computation date* either, only with the effective dating of the underlying
rates. A raise applied today must not move last quarter's Schedule 14.

Also re-check the differencing surface: cost is now a function of effective-dated
rates, so an actor able to query the same aggregate across two effective dates
could difference it. Leak tests must probe **across** effective dates.

### E3 — Classification and compliance fields

Additive columns, no new correctness risk:

- `pay_type` gains `exclude_from_payroll` as a third value — **excluded from
  Schedule 14 labor cost entirely**, not defaulted into hourly or salary.
  Four El Sendero staff carry it today.
- `full_part_time`, distinct from exempt status
- `i9_submitted_on`, `w4_submitted_on`
- employment status enum (active / inactive / leave / terminated), replacing the
  bare `termination_date`
- data-completeness flag, orthogonal to employment status; blocks pay-run
  inclusion when incomplete

### E4 — PTO policy and accrual

`pto_policy`, `pto_accrual_ledger`, and a derived balance. Accrual runs off
approved timecard hours; PTO taken flows to the Schedule 14 benefits line.

**Research item before planning:** California paid sick leave accrual rules must
be verified against current statute rather than assumed — the accrual rate, the
annual usage cap, and the carryover cap have all been amended in recent years,
and the incumbent's observed policy (`CA Sick Time Policy`, balance rendered as
`20:19` hours:minutes) does not reveal which ruleset it implements. Do not write
the accrual engine off memory.

PTO dollars hit Schedule 14, so the money rule applies: PTO **cost** aggregates
are subject to the same priced-population suppression as wages. PTO **balances**
are per-employee and are not cost, but a balance plus a known policy can imply a
rate if exposed alongside dollars — check this explicitly.

### E5 — Deposit accounts

Replaces the singular sealed `bank_account` / `bank_routing` with a collection:
`deposit_account(employee_id, ordinal, allocation_type ∈ amount|percent,
allocation_value, account_type, sealed_account, sealed_routing)`.

Uses the existing C1 sealed-envelope path — HPKE-sealed client-side, blind
overwrite, no server-readable plaintext, audit on every access. Both provider
adapters (`gusto_adapter.py`, `adp_adapter.py`) need serialization for multiple
accounts with allocation, and the mocks need to accept it.

This is the phase with real PII blast radius; it should ship last and get its
own adversarial review focused on the sealed path.

## Migration strategy

**Backfill then cut over** (chosen). One Alembic migration per phase creates the
new tables, backfills from the retiring columns, and drops them in the same
release. No dual read paths — two live code paths through costing logic is
precisely where the previous Criticals originated.

Backfill sets `effective_from` to `payroll_period_anchor` (2026-01-05).

**Accepted limitation:** rate history prior to cutover is unrecoverable. The
incumbent masks all rate values in its UI and its native export is broken, so
there is nothing to import — effective dating begins at cutover. This is
recorded here so nobody later mistakes the absence of history for a bug.

## Out of scope

- Multi-state working (`Working State #2`) — CA-only operation
- EEO / gender demographics — a distinct sensitivity class that should be sealed
  and access-separated from payroll; deferred deliberately rather than folded in
- Garnishments and deductions beyond deposit allocation
- Importing terminated employees' historical job titles — the incumbent clears
  `Position` on deactivation, so that data is already lost upstream

## Risks

| Risk | Mitigation |
|---|---|
| Suppression regressions from the new priced population | Fresh leak tests per phase; adversarial review before each merge, as with A–D |
| Silent restatement of closed periods | Byte-identical-across-computation-date regression in E2 |
| CA sick leave rules implemented from memory | Explicit research gate before E4 planning |
| Sealed-PII regression in E5 | Dedicated adversarial review of the sealed path; no plaintext in exception messages (the C2 lesson) |
| Scope size — five phases, largest pillar to date | Each phase independently shippable and green; stop after any phase without leaving the system inconsistent |
