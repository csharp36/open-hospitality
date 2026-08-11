# Property configuration: room inventory and fiscal calendar — design

Status: **APPROVED design**, ready for an implementation plan.
Issue: [#8 — Property configuration: room inventory and fiscal calendar](https://github.com/csharp36/open-hospitality/issues/8) (milestone: Analytics; *good first issue*).

## 1. Purpose & principle

Every performance statistic in the Analytics milestone divides by *rooms
available*. The platform has no authoritative record of how many sellable rooms
a property has, how that number changes over time, or what the property's fiscal
calendar is. Without it, occupancy and RevPAR cannot be computed, and any number
derived from them is wrong in a way nobody notices until an owner argues with it.

This slice creates that authoritative record — room inventory (effective-dated),
out-of-order rooms (date-ranged), and a fiscal calendar (calendar-month or
4-4-5) — behind the established multi-tenant walls, and a basic settings form to
edit it. It is the hard dependency of [#9 — Core performance
statistics](https://github.com/csharp36/open-hospitality/issues/9).

**Posture (adr-010, fail-closed and loud):** an unconfigured fiscal calendar, or
a rooms-available window reaching dates before a property's first inventory
record, **refuses with a named error** — it never defaults to a guess or a zero.
The same stance `overtime_rules.rules_for(None)` takes for a missing wage
jurisdiction. The demo seed configures both properties so nothing refuses in the
demo.

## 2. Scope

In scope:

- Property-level room inventory: total sellable rooms, **effective-dated** so a
  renovation or room-type change does not silently rewrite history.
- Out-of-order / out-of-service room tracking by date range, with a reason code.
- Fiscal calendar: calendar-month **or** 4-4-5, with a fiscal-year start month;
  periods resolve by key to concrete date ranges.
- Alembic migration (with RLS) plus demo seed for the existing demo properties.
- A basic settings form to view and edit all of the above.

Out of scope (issue-stated): room-type-level inventory; any UI beyond a basic
settings form. Also deferred by this design: consuming any of these numbers to
compute occupancy/ADR/RevPAR — that is #9.

## 3. Data model

Three new tables, all `OrgScoped` (org-stamped + Postgres RLS) with a composite
`(org_id, property_id)` foreign key to `property` — the pattern `PaySchedule`,
`Department`, and `LaborStandard` already follow, which closes the cross-tenant
"squat another org's property slot" class the composite FK exists for.

### 3.1 `room_inventory` — effective-dated sellable-room count

| column | type | notes |
|---|---|---|
| `inventory_id` | int PK | |
| `org_id` | int | `OrgScoped`; part of the composite FK |
| `property_id` | str | composite FK `(org_id, property_id)` → `property` |
| `effective_date` | date | the date this count takes effect |
| `total_rooms` | int | `CHECK (total_rooms > 0)` |
| `created_at` | ts | `server_default=now()` |

- Unique `(property_id, effective_date)` — one count per property per effective
  date.
- **Append-only, "greatest-effective-date-≤-D" semantics.** To change the count
  you insert a new row; the count in force for any date `D` is the row with the
  largest `effective_date ≤ D`. History is never rewritten. A correction to a
  mistyped entry is an upsert on `(property_id, effective_date)` — see §5.
- *Rejected alternative:* explicit `[start_date, end_date)` ranges per count. It
  forces a mutation of the prior row's `end_date` on every insert, reopening the
  "silently rewrite history" risk and inviting gaps/overlaps. The append-only
  temporal shape is strictly simpler and matches the issue's language.

### 3.2 `out_of_order_room` — OOO / OOS blocks

| column | type | notes |
|---|---|---|
| `ooo_id` | int PK | |
| `org_id` | int | `OrgScoped` |
| `property_id` | str | composite FK `(org_id, property_id)` |
| `start_date` | date | inclusive |
| `end_date` | date | inclusive; `CHECK (end_date >= start_date)` |
| `room_count` | int | rooms out in this block; `CHECK (room_count > 0)` |
| `reason_code` | str(20) | **closed vocabulary** via `CHECK` |
| `note` | str(200) \| None | optional free text, bounded |
| `created_at` | ts | `server_default=now()` |

- Reason vocabulary (mirrors the `org_settings.crm_provider` CHECK idiom):
  `maintenance`, `renovation`, `damage`, `deep_clean`, `other`. Defined once as a
  Python constant and asserted equal to the DB CHECK.
- Blocks may overlap the query window partially, and may overlap **each other**
  (two independent renovations). Room-nights sum across blocks (see §4.1); there
  is deliberately no de-duplication of overlapping blocks — two blocks of 3 rooms
  on the same night are 6 room-nights out, because they are different rooms. (A
  known modelling limit: without room-type/room-number identity we cannot detect
  a *double-counted* physical room. Acceptable for this slice; noted so #9 does
  not assume otherwise.)

### 3.3 `fiscal_calendar` — one row per property

The `PaySchedule` precedent: a per-property config singleton in its own table.

| column | type | notes |
|---|---|---|
| `property_id` | str PK | one row per property |
| `org_id` | int | FK → `organization` |
| `calendar_type` | str(20) | `CHECK IN ('calendar_month','445')` |
| `fiscal_year_start_month` | int | `CHECK (1 <= x <= 12)` |
| `week_start_weekday` | int \| None | 0–6, Monday=0 … Sunday=6 (Python `date.weekday()` convention); required for `445`, NULL for `calendar_month` |
| `created_at` | ts | `server_default=now()` |

- **Paired CHECK** (the biometric-notice-pairing idiom): `week_start_weekday IS
  NOT NULL` iff `calendar_type = '445'`. A `445` row without a weekday, or a
  `calendar_month` row with one, is refused by the schema independently of the
  API.

## 4. Query services (pure, computed-not-materialized)

No materialized period rows. Periods and availability are resolved on demand by
pure functions — the codebase's one-predicate-one-function ethos, so there is one
source of truth and nothing to keep in sync.

### 4.1 `inventory.py`

- `total_rooms_on(session, property_id, date) -> int`
  The in-force count: the greatest-`effective_date`-≤-`date` row's `total_rooms`.
  Raises `InventoryNotConfigured` if `date` precedes the property's first record.

- `rooms_available(session, property_id, start, end) -> int`
  The headline acceptance criterion. Returns **room-nights available** over the
  inclusive window `[start, end]`:

  ```
  rooms_available = Σ_day∈window ( total_rooms in force that day )
                  − Σ_block ( overlap_days(block, window) × block.room_count )
  ```

  - **Mid-window inventory change:** the first sum walks the in-force inventory
    rows across the window, so a count that changes on day *N* is counted
    correctly on both sides of *N*.
  - **Partial OOO overlap:** each block is clamped to the window before
    multiplying; a block half inside contributes only its inside days.
  - **Leap year:** pure date arithmetic, so 29 Feb is simply another day in the
    window — the leap-year criterion falls out with no special case.
  - Refuses (`InventoryNotConfigured`) if **any** day in the window lacks an
    in-force inventory row — the denominator is unknown, so it will not fabricate
    one.

### 4.2 `fiscal.py`

Resolves a property's `fiscal_calendar` config to concrete dates.

- **Period key format:** `"{fiscal_year}-P{NN}"`, where `fiscal_year` is the
  calendar year in which the fiscal year **starts**, and `NN` is `01`–`12`. Both
  calendar-month and 4-4-5 calendars have 12 periods. Example, FY start = July:
  `2026-P01` = July 2026, `2026-P07` = January 2027.

- `resolve_period(config, period_key) -> (start_date, end_date)` (inclusive):
  - *calendar_month:* period *N* is the *N*-th calendar month counting from
    `fiscal_year_start_month`.
  - *445:* the fiscal year's concrete anchor = the first `week_start_weekday`
    **on or after** `date(fiscal_year, fiscal_year_start_month, 1)`. Periods are
    consecutive week blocks in a 4/4/5 pattern per quarter (P1=4 wk, P2=4 wk,
    P3=5 wk, P4=4 wk, …, P12=5 wk) = 52 weeks. **53-week years** (the next
    fiscal year's anchor falls 53 weeks out) are handled by the **final period
    (P12) absorbing the extra 6th week**; this rule is documented in the module
    and in `docs/`.

- `period_containing(config, date) -> period_key` — the inverse; the period a
  given date falls in. This is what #9 uses to bucket a staged transaction.

- `periods_in_year(config, fiscal_year) -> list[(period_key, start, end)]` —
  enumeration for the settings-form preview and for drill-through.

- Raises `FiscalCalendarNotConfigured` when the property has no config row.

`period_containing` and `periods_in_year` are **defined in terms of**
`resolve_period`, so period boundaries have a single source of truth (kills the
"two implementations drift" class).

## 5. API

All routes hang off `/api/properties/{property_id}/…` and follow the
`POST /api/departments` template.

**Auth (existing helpers, unchanged):**

- **Writes:** `require_grants(ORG_ADMIN, PROPERTY_GM)` + `_require_onboardable_property(property_id)`
  — org-fenced write-confinement via `assignment_scope` (org_admin bypasses;
  a GM is confined to assigned properties). A single 403 with no existence
  oracle (`_refuse_property`) for out-of-scope / other-org / nonexistent.
- **Reads:** `_require_readable_property(property_id)` — the read gate that also
  proves "is it here" so an empty result never doubles as a refusal.
- Every route is `require_active_org`-bound; RLS is the backstop beneath all of
  it.

| Method | Path | Body / query | Auth |
|---|---|---|---|
| `GET` | `/{pid}/config` | → inventory history + OOO list + fiscal config | read |
| `GET` | `/{pid}/rooms-available` | `?start=&end=` → `{ room_nights, start, end }` | read |
| `GET` | `/{pid}/fiscal-periods` | `?fiscal_year=` (enumerate) · `?period=` (one) · `?date=` (containing) | read |
| `POST` | `/{pid}/inventory` | `{ effective_date, total_rooms }` — upsert on `(pid, effective_date)` | write |
| `POST` | `/{pid}/out-of-order` | `{ start_date, end_date, room_count, reason_code, note? }` | write |
| `DELETE` | `/{pid}/out-of-order/{ooo_id}` | remove a block | write |
| `PUT` | `/{pid}/fiscal-calendar` | `{ calendar_type, fiscal_year_start_month, week_start_weekday? }` — upsert (one/property) | write |

- Room-inventory rows are **append-only** (no DELETE). A wrong count is corrected
  by POSTing the same `effective_date` (upsert overwrites `total_rooms`). OOO
  blocks get a DELETE because a mistyped block is an error, not history.

**Boundary validation** (loud 4xx, no leaks): `total_rooms`/`room_count` > 0;
`end_date >= start_date`; `reason_code ∈ vocab`; `fiscal_year_start_month ∈
1..12`; `week_start_weekday ∈ 0..6`. `calendar_type=445` **requires**
`week_start_weekday`; `calendar_month` **rejects** it — the API mirrors the
paired DB CHECK so a bad combination fails at the edge, not deep in a query.

**Audit** (the CRM-refresh idiom): every write emits one `AuditEvent` — `action ∈
{property_inventory_set, ooo_added, ooo_removed, fiscal_calendar_set}`,
`resource_type="property"`, `resource_id=property_id`. A refusal that *passed
confinement* audits too, with `session.rollback()` **before** the audit write so
the audit commit can never sweep in a partial mutation (the rollback-before-audit
invariant).

## 6. Migration & seed

**Migration** — one Alembic revision on the current head:

- Creates the three tables with their CHECKs, composite `(org_id, property_id)`
  FKs, and uniques.
- **RLS:** `ENABLE` + `FORCE ROW LEVEL SECURITY` on all three, with the `USING` +
  `WITH CHECK` `org_id = current_setting(...)` policies (identical to
  `l2a0rlswall`), and grants the non-owner `usali_app` serving role. This
  satisfies the "RLS applies to all new tables" criterion.
- **Populated-module downgrade pins** per convention: the downgrade drops the
  tables; the migration test carries seeded rows through upgrade → downgrade →
  upgrade.
- **No backfill** — these are new config facts; inventing history would fabricate
  room counts nobody stated.

**Seed** — the two demo properties, in `scripts/demo_seed.py` beside the existing
`PaySchedule` loop (its precedent for dated per-property config). Chosen to
exercise both fiscal paths and the effective-dated + OOO logic in the live demo:

| Property | Room inventory | Fiscal calendar | OOO |
|---|---|---|---|
| **HISJ** | 140 rooms (early effective date) **and a later change to 138** (renovation) — exercises the effective-dated + mid-period-change paths | `calendar_month`, FY start = January | one block: 3 rooms, `renovation`, a one-week range |
| **SSSJ** | 90 rooms (single row) | `445`, FY start = January, week starts Sunday | none |

Room counts are invented-but-plausible; both properties are already
fictitious-by-construction in this repo. This gives #9 a property on each
calendar type and a non-trivial inventory/OOO history to compute against.

Seeded in `demo_seed.py` rather than `mapping/properties.yaml` because inventory
and OOO are *dated facts* (a mid-history change, a date range) that YAML
property-identity rows do not model well — the `PaySchedule` precedent already
places dated per-property config in the seed script.

## 7. Frontend — basic settings form

A "Property configuration" settings surface, gated `org_admin | property_gm` to
mirror the API, following the existing Employees/Departments page patterns and
the brass/pine design tokens. One section per concern:

- **Room inventory** — current in-force count + effective date; an "add a count"
  form (`effective_date`, `total_rooms`); a compact append-only history list
  (newest first). Re-submitting an existing date reads as a correction.
- **Out-of-order rooms** — list of active/future blocks; an add form (date range,
  room count, reason dropdown from the closed vocab); a remove control per block.
- **Fiscal calendar** — `calendar_type` radio (Calendar month / 4-4-5); FY
  start-month select; a **week-start-weekday select shown only when 4-4-5 is
  chosen** (mirrors the paired DB constraint). A read-only preview line resolves
  the *current* fiscal period to its date range so the operator can see the
  config is right.

No stats, no drill-through, no room-type breakdown — the issue's "basic settings
form" upper bound.

## 8. Test plan

TDD — tests precede implementation. Every acceptance criterion is pinned:

| Criterion | Test |
|---|---|
| Effective-dated; past-date returns in-force count | `total_rooms_on` before/after a change; a date between two records |
| `rooms_available = total×days − OOO room-nights` | formula on seed data; **partial-overlap** OOO clamped to window; **mid-window inventory change** |
| Fiscal periods resolve by key, both types | `resolve_period` calendar-month + 4-4-5; `period_containing` inverse agrees; **53-week year** final-period absorption |
| RLS on new tables; cross-tenant isolation | two-org world (`two_tenant_world` / `rls_client`): tenant B cannot read tenant A's inventory / OOO / fiscal rows |
| Mid-period change + **leap year** | `rooms_available` across a count change; a window spanning 29 Feb counts the extra day |
| Boundary / validation | reason-code vocab CHECK; `445`-requires-weekday (API + DB paired CHECK); `effective_date` upsert correction; refuses-loudly when unconfigured |
| Frontend | vitest: week-start field appears only for 4-4-5; reason dropdown; add/remove OOO |

Migration test carries seeded rows through upgrade → downgrade → upgrade.
Consistent with project culture, the closing gate is a three-lens adversarial
review (disclosure / tenancy, correctness, migration+tests) with mutation checks
— run after the build, not part of this plan.

## 9. Decisions resolved during design

- **Slice scope:** full issue including the basic settings form (one PR).
- **Effective-dated model:** append-only, greatest-≤-date (not explicit ranges).
- **4-4-5 anchor:** start month + week-start weekday, concrete anchor computed
  per year (first weekday on/after the 1st of the start month); 53-week year's
  final period absorbs the extra week.
- **Fiscal periods:** computed on demand, not materialized.
- **OOO reason code:** closed vocabulary via DB CHECK.
- **Unconfigured state:** refuse loudly (fail-closed), never default.

## 10. Open (non-blocking) follow-ons

- Room-type / room-number-level inventory (out of scope here) would let #9 detect
  a physically double-counted OOO room; recorded as a known limit in §3.2.
- Making `punch_business_day_cutoff_hour` per-property (today a global
  `Settings` constant) is unrelated to this slice but is the other obvious
  per-property config gap; not pulled in.
