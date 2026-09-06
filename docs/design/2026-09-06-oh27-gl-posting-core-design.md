# OH-27 — general-ledger posting core (design)

Status: **DRAFT — for review (2026-09-06).** Implements
[ADR-011](../adr/adr-011-gl-posting-model.md) (accepted 2026-09-06), the first
GL slice per [`ROADMAP.md`](../ROADMAP.md) Tier 0 #3. Everything ADR-011
decides is inherited here without re-argument: per-org chart seeded from a
USALI template, append-only journal with reversal-only correction enforced by
grants, a deterministic idempotent posting engine, Decimal-exact amounts, and
audited per-property period close. This document decides the shapes that
implement those rules.

The starting asset is `qbo_push.build_journal_entry`: a working, balanced,
bucketed, content-hashed journal-entry builder that today has no journal to
post into and hands its output to QuickBooks. OH-27 is, at heart, giving that
plan a home and generalizing who may produce one.

## 1. Goal

A property's daily activity becomes double-entry journal entries in OH's own
immutable journal; a trial balance and balance sheet read from that journal;
fiscal periods close and refuse late postings; and the QBO push becomes an
export *from* the journal, so nothing owners rely on today breaks. The
Summary Operating Statement is **not** re-pointed in this slice — that
cutover is gated behind a parity check (D-OH27.9), because today's SOS is
deliberately revenue-side from the fact tables while the journal will carry
labor expense too, and a silent widening of the statement is exactly the
kind of drift D8.3 forbids.

## 2. Decisions log

- **D-OH27.1 — `gl_account` is an `OrgScoped` table seeded from a repo
  template (PROPOSED).** Per ADR-011 §1. The template is
  `mapping/gl_accounts_usali.yaml`, the successor to the placeholder
  `mapping/qbo_accounts.yaml` (whose own header says it awaits a real
  chart). Columns: `account_code`, `name`, `account_type` (`asset`,
  `contra_asset`, `liability`, `equity`, `income`, `expense` — normal
  balance derives from type), nullable USALI linkage
  (`usali_schedule_id`, major/sub/line), nullable `system_role`, and
  `is_active`. Seeding happens at org provisioning; for orgs that already
  exist, an idempotent operator command (`usali gl seed-chart`) — NOT an
  every-deploy seed, per the OH-17 lesson that a re-running seed silently
  reinstates what an operator changed.

- **D-OH27.2 — System accounts are found by role, not by number
  (PROPOSED).** `build_journal_entry` hardcodes `"1210"` as the balancing
  account, and the YAML header warns that renumbering breaks the builder.
  With per-org charts that constant becomes wrong the first time an org
  renumbers. `system_role` (unique per org where set:
  `guest_ledger_clearing`, later `accrued_payroll`) is how the engine finds
  structural accounts; an org may renumber freely and may not delete a
  role-bearing account. Missing role at posting time is a loud refusal
  naming the role.

- **D-OH27.3 — Journal shape: `journal_entry` + `journal_line`, direction
  as posting type, amounts always positive (PROPOSED).** The `JeLine`
  convention, kept: each line has `posting ∈ {Debit, Credit}` and a positive
  `Numeric(15,4)` amount mapped as `Decimal` — direction never lives in the
  sign, which is also what the QBO export body requires. `journal_entry`
  carries org, property, `business_date`, `source_type` + `source_hash`
  (D-OH27.5), `posted_at`/`posted_by`, an optional `reversal_of` self-FK,
  and a `memo`. `journal_line` carries the entry FK, the `gl_account` FK,
  posting, amount, memo, and a nullable `fact_id` link so drill-through
  runs statement → journal line → fact → staged PMS row.

- **D-OH27.4 — Balance and immutability are enforced in Postgres
  (PROPOSED).** Per ADR-011 §2 and §4. A deferred constraint trigger sums
  each entry's lines at commit and refuses `debits != credits` — exact
  Decimal comparison, no epsilon. The migration REVOKEs UPDATE and DELETE
  on both journal tables from the application role; tests exercise the
  refusal directly (§8). The Python-side balance check in the builder stays
  as the early, friendlier error; the trigger is the wall.

- **D-OH27.5 — A posting-source registry, proven by two sources
  (PROPOSED).** The checklist/`CRM_PROVIDERS` idiom: a module-level closed
  registry in `gl_posting.py`, each source owning a `build_plan(property,
  business_date)` callable that returns the generalized `JePlan`. Two
  sources ship in this slice, per the ADR-009 discipline that one shape
  cannot prove a seam:
  - `pms_daily` — `build_journal_entry`'s logic, lifted out of `qbo_push`:
    revenue / pass-through taxes / settlements / non-operating buckets, the
    guest-ledger-clearing remainder, unchanged economics.
  - `payroll_accrual` — an approved pay run posts gross wages by department
    to the Schedule 14 expense accounts against the `accrued_payroll`
    liability role, from the labor facts the pay run already aggregates.
    Deliberately minimal: employer taxes and benefits accruals wait for
    OH-29/OH-30 territory; the source exists to prove the registry and to
    put real labor expense in the journal.

- **D-OH27.6 — Idempotency ledger with reverse-and-repost (PROPOSED).**
  A `gl_posting_ledger` table on the `QboPushLedger` precedent: one row per
  (property, business_date, source_type) recording the last posted
  `source_hash` (the existing canonical-content sha256) and entry id. Same
  hash → no-op. Changed hash in an **open** period → post the reversal of
  the prior entry and the corrected entry, atomically, and update the
  ledger row. Changed hash in a **closed** period → refuse, naming the
  period (D-OH27.7). This is deliberately different from the QBO push's
  `stale` refusal: QBO is someone else's books and OH must not mutate them
  unbidden, but OH's own journal corrects itself by reversal — that is what
  append-only-with-reversals is *for*.

- **D-OH27.7 — Close is an append-only event log; state is derived
  (PROPOSED).** `gl_period_event` rows — (org, property, `period_key`,
  `event ∈ {close, reopen}`, actor, timestamp, reason) — and a period's
  closed/open state is the last event, D-B4.1's derived-not-stored rule
  applied to close. Period keys are `fiscal.py`'s `YYYY-PNN`;
  `period_containing` decides which period a `business_date` falls in, and
  a property with no `FiscalCalendar` row keeps its existing loud
  `FiscalCalendarNotConfigured` refusal — a property cannot post before its
  calendar exists. Posting into a closed period is refused with the period
  key and the reopen route in the message. Reopen is org_admin-only and
  requires a non-empty reason. Close does not require all days posted —
  it asserts "no more postings", not "nothing was missed" — but the API
  response names any fact dates in the period with no journal entry, so
  closing over a gap is a visible choice, never a silent one.

- **D-OH27.8 — History enters by running the engine, not by migration
  (PROPOSED).** The Alembic migration creates tables and grants only. A CLI
  (`usali gl post --property X --from --to`) backfills by invoking the
  posting engine over promoted facts — idempotent via D-OH27.6, so running
  it twice is safe, and the backfill exercises the exact code path
  production uses instead of a second one-shot implementation. Order for an
  existing deployment: seed chart → backfill → verify trial balance →
  close historical periods.

- **D-OH27.9 — Trial balance and balance sheet read the journal now; the
  SOS re-points only after a parity gate (PROPOSED).** New read-side
  queries (in `reporting.py`'s pure-read style) produce the trial balance
  and balance sheet from journal lines by account, per period. The SOS
  keeps reading facts in this slice. The gate for re-pointing it is a
  parity tool that diffs, per (property, period, USALI line), the
  fact-derived SOS against a journal-derived statement restricted to the
  same revenue-side scope — zero diff on real data, then cutover as its
  own small follow-up. Rationale: the journal-derived statement is *more*
  complete (it has labor), and a statement that silently gained expense
  lines the day the journal shipped would be untrustworthy in exactly the
  way reconciliation-minded operators notice.

- **D-OH27.10 — The QBO push exports the journal (PROPOSED).** Per ADR-011.
  `qbo_push` builds its Intuit body from the posted `journal_entry` for
  (property, date, `pms_daily`) instead of re-deriving from facts; the
  entry's `source_hash` remains the `requestid`, so an entry that has not
  changed replays as before, and `QboPushLedger`'s
  pushed/failed/stale lifecycle is untouched. The payroll-accrual source is
  NOT pushed to QBO in this slice — owners' QBO books get exactly what they
  got before, no more — revisited when an owner asks.

- **D-OH27.11 — Decimal at the posting boundary (PROPOSED).** Per ADR-011
  §4. The engine converts fact amounts via `Decimal(str(amount))` quantized
  to `_QUANT_4DP` — the convention `build_journal_entry` already uses — and
  everything from `JePlan` inward is `Decimal`. Fact columns keep their
  float mapping in this slice; tightening them is desirable and separate.

- **D-OH27.12 — Every new table is `OrgScoped` and enters the four RLS
  lists (PROPOSED).** `gl_account`, `journal_entry`, `journal_line`,
  `gl_posting_ledger`, `gl_period_event` — all five join the hand-enumerated
  RLS inventory and the two-org isolation suite. The inventory is manual by
  design; missing it means the wall is unverified on that table.

## 3. Architecture

New module `gl_posting.py` owns the engine: the source registry (D-OH27.5),
plan building (absorbing the builder from `qbo_push.py`), the idempotency
ledger, and the period gate. `qbo_push.py` shrinks to what its name says —
QBO client orchestration over posted entries. `reporting.py` gains the trial
balance and balance-sheet queries. `fiscal.py` is consumed unchanged.

Posting flow, per (property, business_date, source):

1. Resolve the period via `period_containing`; refuse if closed or if no
   fiscal calendar exists.
2. Build the plan; refuse on unmapped GL codes (`UnmappedGlError`, kept) or
   a missing chart account / system role.
3. Consult `gl_posting_ledger`: same hash → no-op; new → post; changed →
   reverse-and-repost (open period only).
4. Post entry + lines and the ledger row in one transaction; the balance
   trigger is the last line of defense.

Triggering: promotion (`ledger_promote` / `process_file`) enqueues posting
for the affected (property, date) after facts land, and the CLI covers
backfill and re-runs. Posting failure never blocks promotion — facts landing
and books posting are separate outcomes, each loud on its own.

## 4. Data model

Five tables, all `OrgScoped` (D-OH27.12):

| Table | Grain | Notes |
|---|---|---|
| `gl_account` | (org, account_code) | Type-derived normal balance; nullable USALI linkage; `system_role` unique per org where set; composite FK to property NOT needed — chart is org-level |
| `journal_entry` | one posting event | property, business_date, source_type, source_hash, posted_at/by, reversal_of, memo |
| `journal_line` | one side of one entry | entry FK, gl_account FK, posting, positive `Numeric(15,4)`, memo, nullable fact_id |
| `gl_posting_ledger` | (org, property, business_date, source_type) | last source_hash + current entry id; the idempotency arbiter (unique constraint = the concurrency wall, the `QboPushLedger` precedent) |
| `gl_period_event` | one close/reopen event | property, period_key, event, actor, reason, at; append-only like the journal |

Grants: application role loses UPDATE/DELETE on `journal_entry`,
`journal_line`, and `gl_period_event` in the same migration that creates
them.

## 5. API

All under org auth; mutation routes org_admin-only, matching the
integrations-page precedent.

- `GET /api/gl/accounts` — the org's chart; `PUT /api/gl/accounts/{code}` —
  add or edit (role-bearing accounts refuse deactivation).
- `GET /api/gl/trial-balance?property=&period=` — journal-derived, with
  per-account drill to entries and lines.
- `GET /api/gl/periods?property=` — periods with derived open/closed state
  and their unposted-fact-date gaps.
- `PUT /api/gl/periods/{period_key}/close` and `/reopen` — idempotent
  event appends (PUT because they set state, the D-B4.5 shape); reopen
  requires `reason`.
- `POST /api/gl/post` — trigger posting for (property, date range); returns
  per-date outcomes in the `push_month` shape (posted / no-op / reposted /
  refused, with messages).

## 6. Frontend

One page this slice: **/gl** — trial balance by property and period, a
period rail showing open/closed with the close/reopen controls
(org_admin-gated like `/integrations`), and drill-through from a trial
balance line to its journal entries and from a line to its source fact. The
balance sheet renders on the same page from the same query. No chart-editing
UI yet — the API exists, the page can wait for a real need.

## 7. Error handling

Refusals, all loud, none silent (ADR-010):

- `FiscalCalendarNotConfigured` — kept as-is; posting inherits it.
- `UnmappedGlError` — kept; message still names the curation path.
- `PeriodClosedError` — names the period key and the reopen route.
- `SystemRoleMissingError` — names the role and the chart-seeding fix.
- Unbalanced entry — the deferred trigger raises; the builder's Python
  check means users see it first as a friendly error, and the trigger
  firing at all is a bug report.
- Reversal-repost is atomic: a failure mid-pair rolls back both, and the
  ledger row still points at the prior entry.

## 8. Testing

- **Immutability by grant:** as the application role, UPDATE and DELETE
  against posted entries/lines and period events fail at the database.
  Named tests, since ADR-011 cites the enforcement point.
- **Balance trigger:** an unbalanced insert crafted below the builder is
  refused at commit; exact-Decimal, not epsilon.
- **Reverse-and-repost invariants:** after any sequence of fact
  re-promotions, (a) the ledger row's entry matches the current facts,
  (b) the journal's net per account equals a fresh plan's net, (c) every
  superseded entry has exactly one reversal.
- **Close semantics:** posting refused into closed, allowed after audited
  reopen; close response names unposted fact dates.
- **RLS:** the two-org isolation suite covers all five tables, through the
  ORM wall and with it bypassed — the OH-17 pattern.
- **Parity:** fact-derived SOS vs journal-derived revenue-side statement,
  zero diff over the seeded sample data — the D-OH27.9 gate, checked in CI
  from day one so drift is caught before the cutover decision.
- **QBO equivalence:** for the same facts, the journal-built Intuit body
  equals the previous fact-built body line for line — the proof that
  D-OH27.10 changes plumbing, not owners' books.

## 9. Out of scope

- **Bank feeds and reconciliation** — OH-28, gated on its own ADR-012
  production items (legal entity, coverage check).
- **AR/AP** (OH-29), **multi-entity, fixed assets, year-end** (OH-30).
- **The SOS cutover itself** — designed here (D-OH27.9), executed as a
  follow-up once the parity gate is green on real data.
- **Employer-tax and benefits accruals** in the payroll source; gross wages
  only, per D-OH27.5.
- **Automated QBO void/amend** — the `stale` posture stands.
- **Fact-column Decimal migration** and **mapping-dictionary tenancy**
  (OH-20's open decision) — adjacent, not this slice.

## 10. Roadmap deltas this slice applies

`OH-22`'s HotelKey work is unaffected. On build start, `OH-27` moves
`planned` → `in-progress` in `.github/roadmap.yml`; on completion, the
ROADMAP §3 Tier 0 table gains its shipped annotation. No new catalogue
entries — the SOS cutover and the payroll-accrual extensions stay inside
OH-27's summary as written.
