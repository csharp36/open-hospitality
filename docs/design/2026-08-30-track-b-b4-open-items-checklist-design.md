# Track B / B4 — tenant lifecycle state and the open-items checklist (design)

Status: **APPROVED — scope FINAL (2026-08-30).** The onboarding milestone's
state container (**OH-1**, catalogued as **OH-18**). Implements
[D8.2](2026-08-16-data-posture-progressive-onboarding-design.md) — per-integration
progressive onboarding — and inherits D8.1 (one always-sensitive data posture)
and D8.3 (an unconfigured integration is *off*, never mock). Sequenced first by
[`docs/ROADMAP.md`](../ROADMAP.md) §6 because it is the container that OH-17
(per-tenant integration config), operator alerting, and OH-19 (billing) all
attach to.

## 1. Goal

Give every tenant an honest, resumable answer to **"what is left to set up?"** —
computed from what is actually configured, never from a stored copy that can go
stale. D8.2 states the success condition exactly: *"fully prod" is not a state,
it is zero open items.*

Nothing in this slice blocks a tenant from using the product. Per D8.2 the core
— upload a PMS export, get real USALI reporting — needs **zero** integrations
configured, and the checklist reports on setup without gating it.

## 2. Decisions log

- **D-B4.1 — Status is derived, not stored (CONFIRMED 2026-08-30).** An item's
  open/closed status is **computed on read** by probing the underlying
  configuration. The only persisted rows are dismissals, which cannot be
  derived. Rationale: a stored status is a second copy of the truth, and a row
  reading `payroll: connected` can outlive the credential being revoked — the
  precise drift D8.3 forbids. There is no reconciliation job because there is
  nothing to reconcile.

- **D-B4.2 — Registry of item objects with a `probe` callable (CONFIRMED
  2026-08-30).** A module-level closed set in `checklist.py`, each entry owning
  a small probe function. Follows the `CRM_PROVIDERS` idiom
  (`crm_feed.py:53`). Adding an item is a local change in one app-layer list
  (plus its schema mirror, §5) rather than a hunt through modules. Rejected:
  decorator self-registration (item set becomes import-order dependent, no
  single place to read the list) and
  DB-resident item definitions (a second source of truth, contradicting
  D-B4.1 — every item needs code to probe it regardless).

- **D-B4.3 — The probes ignore process-wide `Settings` (CONFIRMED
  2026-08-30).** For payroll and accounting the probe does **not** consult
  `settings.payroll_provider` / `settings.qbo_*`. A deployment-wide credential
  is not *this tenant's* connection, so for a real tenant the honest answer is
  "not connected". Until OH-17 lands, both probes return `open`
  unconditionally — the correct answer under D8.3, not a placeholder.

- **D-B4.4 — `done` outranks a dismissal (CONFIRMED 2026-08-30).** The override
  is consulted **only when the probe says `open`**. An operator who dismisses
  payroll in August and connects it in March sees `done`, not a stale
  "dismissed". Storing the dismissal as an absolute state would reintroduce
  exactly the drift D-B4.1 exists to prevent.

- **D-B4.5 — Dismissal endpoints are idempotent, and use PUT (CONFIRMED
  2026-08-30).** Two browser sessions dismissing the same item concurrently
  must not produce a primary-key violation. The insert is `ON CONFLICT DO
  NOTHING`; deleting an absent override is a 204 no-op. Because the operation
  sets state rather than performing an action, the verb is **PUT**, matching
  the existing set-a-value shape at `property_config_api.py:319` and `:359`.

- **D-B4.6 — Billing and the trial clock are OUT (CONFIRMED 2026-08-30).**
  D8.2 names billing as an open item that becomes required at trial end, but
  with no billing to enforce, `trial_ends_at` would be a column nothing reads
  and a billing item would always probe "not applicable". Both land with
  **OH-19**, where they have a consumer. YAGNI, deliberately.

- **D-B4.7 — The tenant carries no lifecycle status column (CONFIRMED
  2026-08-30).** D8.2 retired the sandbox/prod tier. "Fully prod" is the
  derived predicate `open_count == 0` (`all_clear`), not an enum on
  `organization`. Adding a status column would resurrect the promotion model
  D8 explicitly killed.

## 3. Architecture

Three pieces:

1. **`src/usali/checklist.py`** — the `ITEMS` closed set plus the probes, pure
   over a `Session` and free of HTTP concerns.
2. **The probes** — one scoped query each, run under the caller's
   already-org-bound session, so both tenancy walls apply for free. A probe
   *cannot* observe another tenant's rows even if written carelessly.
3. **`OrgChecklistOverride`** — the sole persisted state, written only on
   dismissal.

```
GET /api/checklist
  └─ for each item in ITEMS:
       done = item.probe(session)          # one scoped query
       if done:                 status = "done"          # D-B4.4
       elif override_exists:    status = "dismissed"
       else:                    status = "open"
  open_count = count(status == "open")
  all_clear  = open_count == 0
```

`ChecklistItem` is a frozen dataclass: `key`, `title`, `description`,
`required`, `where` (the SPA route that closes it), `probe`.

## 4. The registry

**Required — cannot be dismissed:**

| key | title | probe |
|---|---|---|
| `first_report` | Upload your first PMS report | any `IngestBatch` row for the org |
| `room_inventory` | Set sellable room inventory | a `RoomInventory` row for every property, **and at least one property** |
| `fiscal_calendar` | Define the fiscal calendar | a `FiscalCalendar` row for every property, **and at least one property** |

**Optional — dismissible; "never" is a legitimate answer (D8.2):**

| key | title | probe |
|---|---|---|
| `payroll` | Connect payroll | OH-17; `open` until then (D-B4.3) |
| `accounting` | Connect QuickBooks Online | OH-17; `open` until then (D-B4.3) |
| `demand_feed` | Connect a demand feed | `org_settings.crm_provider != ''` |
| `team` | Invite your team | more than one distinct `keycloak_subject` holds a grant |

`room_inventory` and `fiscal_calendar` are **required** because
[`performance-metrics.md`](../reference/performance-metrics.md) makes
`rooms_available` the authoritative divisor for every occupancy and RevPAR
figure and **fails loud** without it. An operator missing those has a product
that refuses to compute — exactly what a setup checklist exists to catch before
they hit it.

Both per-property probes require **at least one property** before they can
report `done`: an org with no properties must not satisfy them vacuously, which
a bare "every property has a row" test would do. B1 creates the first property
at signup, so this is a guard against a partially-provisioned tenant rather
than a common path.

`demand_feed` is the only item that can answer truthfully today, because
`crm_provider` is the one integration already modelled per-org.

**Known gap (recorded 2026-08-30, found in review):** `demand_feed`'s `where`
points at `/schedule`, but that page only *displays* demand already pulled —
nothing in the SPA sets `org_settings.crm_provider`. There is no connect
surface for it anywhere today. So the one item a tenant could genuinely close
is the one with no UI to close it. This is accepted for the backend slice
rather than papered over: the item reports its true status, and the operator
is simply routed somewhere unhelpful. **The frontend plan must resolve it** —
either by routing the item to a real connect control, or by rendering it
without an actionable link and saying why. **OH-17** is what eventually
supplies that surface for all three integrations.

## 5. Data model

```
org_checklist_override          (OrgScoped, Base)
  org_id      PK, FK organization      -- both walls + an org_wall RLS policy
  item_key    PK, String(40)           -- CHECK mirrors the ITEMS keys
  note        String(200) nullable     -- bounded operator text, never logged
  created_at  server_default now()
  created_by  String(64)               -- Keycloak subject, for audit
```

**Presence of the row means dismissed.** There is no `state` column: with one
legal value it could only ever say one thing, and un-dismissing is a row
delete. A future "snooze until" would add a nullable timestamp rather than an
enum.

The `item_key` CHECK is the **schema mirror** of `ITEMS`, kept literal on
purpose so the database refuses an unknown key independently of the app import
— the same discipline `org_settings.crm_provider` follows
(`models.py:489–497`). Adding an item therefore touches two places, and the
mirror comment must say so.

`OrgScoped` puts the table in the L1 `org_id` inventory and the RLS policy
inventory automatically; both invariant tests fail loudly if the migration
forgets the policy.

## 6. API

```
GET    /api/checklist                    operator-gated (router default)
PUT    /api/checklist/{key}/dismissal    require_grants(ORG_ADMIN)
DELETE /api/checklist/{key}/dismissal    require_grants(ORG_ADMIN)
```

The router lives in its own module, `src/usali/checklist_api.py`, mounted in
`server.py` beside the other feature routers (`crm_api`,
`property_config_api`, `sick_leave_api`). It is NOT added to `portal_api.py`,
which is past 1200 lines already.

`GET` returns:

```json
{
  "items": [
    {"key": "first_report", "title": "...", "description": "...",
     "required": true, "status": "done", "where": "/upload"}
  ],
  "open_count": 4,
  "error_count": 0,
  "all_clear": false
}
```

**`all_clear` requires zero open items AND zero errors** (corrected 2026-08-30
in review). An item whose probe failed is not a finished item: the first draft
computed `all_clear = open_count == 0` with errors excluded from the count, so
a tenant whose probes ALL failed would have been told setup was complete when
the truth was simply unknown — the plausible-looking wrong result ADR-010
refuses, and §8's own principle violated one level up. `error_count` is on the
wire so a client can distinguish "4 things to do" from "4 things we could not
check" and say so.

Refusals, per the fail-loud posture of
[ADR-010](../adr/adr-010-fail-closed-loud-posture.md):

- dismissing a **required** item → **422**, with the reason. Never a silent
  no-op.
- an **unknown** `key` → **404**.
- dismissal requires `ORG_ADMIN` because "we don't use payroll" is a standing
  commitment about the tenant, not a per-user preference. Reading the checklist
  needs only the router's operator gate.

## 7. Frontend

- **`/setup`** — a top-level sidebar entry with a count badge, above the
  Accounting section (it belongs to neither Accounting nor Employee
  Management). Required and optional items grouped separately; every item links
  to the route named by `where`; optional items carry a dismiss control.
- **Dashboard card** — rendered while **`all_clear` is false** (NOT while
  `open_count > 0`: on a total probe failure `open_count` is zero while
  nothing is actually known, and gating on it would retire the card exactly
  when the operator most needs it). An `error_count > 0` state must read as
  "we could not check these", never as progress. Compact, links to
  `/setup`. It **retires at zero** and the dashboard returns to pure
  operations, while `/setup` remains the permanent home for the day someone
  reconnects an integration. This is the resolved placement: the checklist page
  is the destination, the dashboard card is a first-run pointer to it.
- All three surfaces (page, card, sidebar badge) share **one** TanStack Query
  key, so they are a single fetch and cannot disagree.

D8.2's confetti at zero open items is *enabled* by `all_clear` but is not built
in this slice.

## 8. Error handling

A probe that raises degrades **that item** to status `error`, with the reason
surfaced in the UI and logged. It does not fail the whole request.

This is a considered reading of ADR-010, not an exception to it: the refusal is
loud and visible, but contained. Blinding an operator to six healthy items
because the seventh's query failed is fail-closed taken past the point of
usefulness. The invariant that matters holds absolutely — **a failed probe
never renders as `done`.**

**The handler MUST call `session.rollback()` before continuing** (the house
pattern at `ingestion.py:273` and `crm_api.py:124`). Probes query a real
Postgres session: a DBAPI-level failure leaves SQLAlchemy in *pending
rollback*, so without the rollback every subsequent probe raises
`PendingRollbackError`, is caught by the same handler, and degrades too — one
flaky probe would blind the operator to every remaining item, the exact
outcome this section exists to prevent. Containment is a property of the
rollback, not of the `try/except`.

*Added 2026-08-30 after code review. The first draft of this section claimed
containment without naming the call that delivers it, and the original
regression test could not detect the difference because its second probe never
touched the session. A test for this must have a later probe issue a real
query.*

**Probes are read-only.** Nothing in the registry writes, and the containment
argument above assumes it: a probe leaving uncommitted writes would make one
item's failure visible to the next in ways the rollback would then discard.
A future probe that needs to write must state why, and own its transaction.

## 9. Testing

- **Per-probe unit tests** against a fake session, including the "no properties
  yet" and "some properties configured" boundaries for the two per-property
  probes.
- **Tenancy:** a two-org test that org A's dismissal is invisible to org B, and
  confirmation that `org_checklist_override` joins the L1 `org_id` inventory
  and the RLS policy inventory.
- **Concurrency:** two dismissals of the same key in one test do not raise —
  the second is a no-op (D-B4.5).
- **Precedence:** an item that is both dismissed and probing `done` reports
  `done` (D-B4.4).
- **API refusals:** the 422 on a required item and the 404 on an unknown key.
- **Frontend:** per the existing `*.test.tsx` convention — the card renders
  only while items are open, and disappears at `all_clear`.

## 10. Out of scope

| Deferred to | What |
|---|---|
| **OH-17** | Per-tenant integration credentials and the QBO/Gusto connect flows. This slice defines the probe interface those will satisfy. |
| **OH-19** | The billing item and the trial clock (D-B4.6). |
| **OH-20** | Tenant-curated USALI mapping. Unmapped transaction codes are a recurring ingestion concern, not a one-time setup item, so they are deliberately not a checklist entry. |
| — | The confetti celebration at zero open items. |

## 11. Plan docs

Backend and frontend split into separate plans, following the B1 precedent:

1. `docs/plans/2026-08-30-track-b-b4-checklist-backend.md`
2. `docs/plans/2026-08-30-track-b-b4-checklist-frontend.md` — written after the
   backend lands.
