# Pillar B — Time & Attendance Design

**Date:** 2026-07-13
**Status:** Approved for planning
**Depends on:** Pillar A (A1 auth, A2.1 property system-of-record, A2.2 workforce model + scoped RBAC + PII/encryption/audit, A2.3 onboarding/provisioning) — all merged.

## Context: the payoff loop

open-hospitality's financial pillar turns PMS reports into USALI facts and a P&L. Pillar A built identity
and the workforce model. **Pillar B closes the loop the whole thesis rests on:** labor is a hotel's
largest controllable cost, and until it lands in the P&L the engine only tells half the story. B captures
time at the pilot, turns punches into approved timecards, and emits **Schedule 14 (Payroll Related
Expenses)** and **Schedule 15 (Payroll / FTE Reporting)** facts into the statement the engine already
produces. `department.usali_schedule_id` (built in A2.2) is the seam it lands on.

Pillar C (embedded payroll rails — bought, not built) later supersedes B's cost *estimate* with the
provider's actual gross-to-net.

## Goal

Staff clock in and out on a shared, passwordless kiosk; punches become biweekly timecards a GM approves;
approved hours × pay rate (with overtime) become labor-cost and hours/FTE facts in the P&L. No payroll
processing, no scheduling, no biometrics.

## Decisions locked with the user (2026-07-13)

1. **B's payoff is hours AND estimated labor cost** — B does not stop at hours. It emits Schedule 15
   (hours/FTE) *and* an estimated Schedule 14 (cost), closing the payoff loop without waiting for Pillar C.
2. **Time capture first; scheduling is a later pillar.** When scheduling comes, its driver is
   **labor-cost/overtime control** (planning hours against a labor-% target), which is the right
   ordering — you cannot forecast labor well without actuals, and B produces the actuals.
3. **Kiosk trust = enrolled device + photo evidence.** An admin enrolls an iPad; the server issues a
   revocable, property-scoped device token. The employee taps their name (**no PIN, no password** — this
   mirrors the pilot's real flow); a live photo is captured as **evidence for GM review**.
   **No face recognition** — photos are never templates, keeping the system out of biometric-privacy
   regimes (BIPA/CCPA).
4. **Overtime modeled; meal-break premiums flagged, not priced.** B computes California daily/weekly
   overtime. It does **not** price meal-break premiums — it raises them as compliance *warnings* on the
   timecard. Pricing (and all gross-to-net) is Pillar C's provider.
5. **Photos live behind a `PhotoStore` seam with a retention clock** — encrypted local filesystem in dev,
   S3 + SSE-KMS in production; purged **90 days after the timecard is approved** (a setting,
   `USALI_PUNCH_PHOTO_RETENTION_DAYS`, not a constant). Photos are evidence to be eyeballed, not an archive.
6. **Biweekly timecards, GM-only approval.** A single approver per property.
7. **Labor facts get their own table** (`usali_labor_fact`) unioned into reporting — not forced into the
   PMS-shaped fact tables (see Architecture).

## Architecture

```
shared iPad kiosk (enrolled; property-scoped device token)
    employee taps name → live photo → Clock In / Lunch Start / Lunch End / Clock Out
        ↓  punch API (device-authenticated)
    punch row + photo pointer ──→ PhotoStore (dev: encrypted FS │ prod: S3 + SSE-KMS)
        ↓  biweekly assembly
    timecard (hours, OT, missed-punch + meal-break warnings)
        ↓  GM approves → locks
    labor promote: approved hours × pay_rate, with OT multipliers
        ↓
    usali_labor_fact (property, business_date, department, hours, ot_hours, est_cost)
        ↓  reporting union
    SOS / P&L: Schedule 14 (payroll expense) + Schedule 15 (hours / FTE)
```

**Why a separate fact table.** Every existing fact is structurally bound to PMS provenance:
`usali_financial_fact` requires a NOT-NULL, unique `stage_id` (FK to `pms_daily_financial_stage`) plus an
`ingest_batch_id`; `usali_statistic_fact` likewise requires `stat_stage_id`. An approved timecard has no
PDF and no PMS stage row. Rather than fabricate PMS-shaped rows (misleading to anyone reading the stage
tables or drilling through) or relax the FKs (breaking the "every fact has provenance" invariant), labor
gets `usali_labor_fact` with lineage to the **timecard**, and the reporting layer unions it into the
statement. Provenance stays honest; the P&L still shows labor.

**Seams.** `PhotoStore` is injected into `create_app` exactly like `TokenVerifier` (A1) and
`KeycloakAdmin` (A2.3) — an in-memory/temp-dir fake in tests, the real store in production. The test loop
stays offline: no S3, no camera.

## Data model (Alembic migration)

- **`kiosk_device`** — an enrolled iPad. `(device_id, property_id, name, token_hash, enrolled_by,
  enrolled_at, last_seen_at, revoked_at)`. The token is stored **hashed** (it is a bearer credential);
  the plaintext is shown once at enrollment.
- **`punch`** — immutable. `(punch_id, employee_id, kiosk_device_id, punch_type, punched_at,
  business_date, photo_key, timecard_id)`. `punch_type ∈ {clock_in, lunch_start, lunch_end, clock_out}`.
  `timecard_id` is NULL until the punch is assembled into a timecard — it gives assembly and the photo
  retention purge a direct join instead of an employee+date-window reconstruction. Corrections are never
  edits — they are `timecard_adjustment` rows.
- **`timecard`** — `(timecard_id, employee_id, period_start, period_end, status, approved_by,
  approved_at)`. `status: open → submitted → approved`. Approval locks it.
- **`timecard_adjustment`** — audited manager corrections (e.g. a missed clock-out).
  `(adjustment_id, timecard_id, punch_type, adjusted_at, reason, actor_subject)`. Also written to
  `audit_event`.
- **`usali_labor_fact`** — `(labor_fact_id, property_id, business_date, department_id, hours, ot_hours,
  est_cost, timecard_id)`. Unique on `(property_id, business_date, department_id, timecard_id)` so a
  re-promote is idempotent rather than double-counting.
- **`employee.pay_rate`** — new column, **encrypted** (A2.2's `EncryptedString` pattern), settable only by
  `payroll_admin`.

## RBAC (reuses A2.2 — no new authorization concepts)

- **Kiosk enrollment / revocation:** `org_admin` or `property_gm` (confined to its own property via
  A2.3's `assignment_scope`).
- **Punching:** the kiosk device token — *not* a user session. A device may only punch employees at its
  own property.
- **Timecard view:** A2.2 scope — a `department_manager` sees its department's timecards, a
  `property_gm` its property's, global roles all.
- **Timecard approval:** `property_gm` (or `org_admin`) only.
- **`pay_rate` read/write:** `payroll_admin` only — the existing segregated PII gate.
- **Labor facts:** department-level aggregates. **Not PII** — visible to finance/GM like any other fact.
  This is the point of the Payroll-Admin segregation: the promote reads individual rates server-side and
  emits only aggregates.

## Key flows

- **Enroll a kiosk:** GM enrolls an iPad → server issues a one-time device token (hash stored) → the iPad
  holds the token; every punch presents it. Revocation is immediate (`revoked_at`).
- **Punch:** the kiosk lists that property's active employees → employee taps their name → camera captures
  a live photo → punch type selected → punch + photo stored. No secret is required from the employee.
- **Assemble + approve:** punches roll into the biweekly timecard; the system computes hours and OT and
  raises warnings (missed punch, missed/late/short meal break). The GM reviews — including the photos —
  corrects via audited adjustments, and approves. Approval locks the timecard and starts the photo
  retention clock.
- **Promote to facts:** approved timecards → hours × `pay_rate` with OT multipliers → `usali_labor_fact`
  rows per (property, business_date, department) → the SOS unions them into Schedule 14/15.

## Business-date attribution (explicit — do not leave to inference)

A punch is attributed to a business date using the **property-local timezone** and a **business-day cutoff
of 04:00 local**: a punch at or after 04:00 belongs to that calendar date; a punch before 04:00 belongs to
the **prior** business date. This deliberately aligns labor with the PMS **night-audit** business date the
revenue facts already use — otherwise an overnight housekeeping or night-audit shift would land labor on a
different day than the revenue it supported, and the P&L would be subtly wrong.

**To confirm with the pilot:** their actual night-audit run time. If it is not ~04:00, the cutoff is a
per-property setting, not a constant.

## Overtime rules (California, the pilot's jurisdiction)

Computed on **approved** hours: daily >8h → 1.5×; daily >12h → 2×; weekly >40h → 1.5×; 7th consecutive
day worked → 1.5× (and >8h on that day → 2×). Exempt employees (`position.flsa_exempt`, built in A2.2) are
excluded from OT. **Meal-break premiums are not priced** — a missed, late, or short (<30 min) meal break
raises a warning on the timecard for the GM. The Schedule 14 amount is therefore an **estimate** and is
labeled as such wherever it surfaces.

## Security & PII

- The punch is **device-authenticated, not user-authenticated**. This is a deliberate, documented trust
  limitation inherited from the pilot's real flow — buddy-punching is deterred by the photo and GM review,
  not by a secret. Anyone specifying a stronger guarantee must add a per-employee credential (rejected:
  friction at shift change, and the pilot uses none today).
- Device tokens are bearer credentials: stored hashed, property-scoped, revocable, and surfaced in plaintext
  exactly once at enrollment.
- Punch photos are face images of employees. They are **encrypted at rest**, access-controlled, and
  **purged on a retention clock** after approval. **No face recognition, ever** — no templates are derived,
  so no biometric-privacy regime (BIPA/CCPA) is engaged.
- `pay_rate` is PII: encrypted at rest, `payroll_admin`-gated, never returned to non-payroll roles, and
  never logged. Reads are audited (A2.2's `audit_event`).

## Testing & gates

- **Offline test loop, as established:** `PhotoStore` fake (temp dir / in-memory) injected into
  `create_app`; no S3, no camera, no real device in tests.
- Assert: a device token only punches its own property (cross-property punch → 403); a revoked token is
  rejected; punches are immutable (corrections create adjustment rows); business-date attribution across
  the 04:00 cutoff (an 01:00 punch lands on the prior business date); OT math (daily 8/12, weekly 40,
  7th day, exempt excluded); meal-break warnings raised but **not** priced; approval locks the timecard;
  the labor promote is idempotent (re-promote does not double-count); `pay_rate` is unreadable by
  non-payroll roles; the photo purge actually deletes.
- **The payoff assertion:** after approving a timecard, the SOS for that property/period shows labor in
  Schedule 14 and hours/FTE in Schedule 15.
- Existing gates unchanged and green: pytest + Testcontainers Postgres, strict mypy (`packages=["usali"]`),
  ruff; frontend tsc/oxlint/vitest/build/Playwright. The financial suites must stay green throughout.

## Implementation phases (one spec, planned as three)

- **B1 — Kiosk time capture:** `kiosk_device` enrollment + revocable device token; `punch` model +
  business-date attribution; `PhotoStore` seam; punch API; the kiosk UI. *Ships: staff clock in/out at the
  pilot with photo evidence.*
- **B2 — Timecards & approval:** biweekly assembly; hours; missed-punch and meal-break warnings; audited
  corrections; GM approval/lock; photo retention purge. *Ships: approved hours.*
- **B3 — Labor cost → USALI facts:** encrypted `pay_rate`; OT computation; `usali_labor_fact`; the
  reporting union into Schedule 14/15. *Ships: the payoff loop closes — labor appears in the P&L.*

## Out of scope (explicit)

- **Scheduling** — shift templates, demand forecasting, labor-% targets, publish/notify. A later pillar,
  driven by labor-cost/overtime control.
- **Face recognition / biometric templates** — permanently rejected, not merely deferred.
- **Meal-break premium pricing**, gross-to-net, tax withholding, pay runs, direct deposit, garnishments —
  Pillar C (bought rails).
- Employee self-service (pay stubs, schedule viewing) — the passwordless population has no login; any
  self-service arrives via a lightweight path once C exists.
- Multi-state wage rules — the pilot is California; the OT engine is written for CA and must not be
  presented as jurisdiction-agnostic.

## Definition of done

A GM enrolls an iPad; staff clock in, take a lunch, and clock out on it with no password; punches carry
photos and land on the correct business date across the overnight cutoff; the biweekly timecard assembles
with hours, overtime, and meal-break warnings; the GM corrects missed punches (audited) and approves, which
locks the card and starts the photo retention clock; approved hours × encrypted pay rate with CA overtime
promote to `usali_labor_fact`; and the property's Summary Operating Statement shows **Schedule 14 payroll
expense and Schedule 15 hours/FTE** — with the cost labeled an estimate pending Pillar C. All gates green.
