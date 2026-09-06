# OH-27 — the /gl page (FRONTEND) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> (fresh subagent per task, full red→green→commit TDD loop, review between tasks).
> Steps use `- [ ]` checkboxes.

**Goal:** Give the operator the one page design §6 specifies — **/gl**: a
trial balance by property and fiscal period, a period rail with the
close/reopen controls (org_admin-gated), drill-through from a trial-balance
line to its journal entries and from a line to its source transaction, and a
balance sheet rendered from the same query — over the shipped `/api/gl`
surface, plus the three additive backend deltas §11 names.

**Architecture:** One backend task lands first: a journal-entries
drill-through endpoint (none exists), `entry_id` on `PostOutcomeModel`, and
period date bounds on `PeriodModel` (today `get_periods` iterates
`fiscal.periods_in_year` and discards each period's start/end — the page
needs them to label the rail and to post a period's range). Then a `GlPage`
at `/gl` where the **period rail is the period picker**: a row of period
chips, each carrying its derived open/closed state, selection persisted in a
`?period=` search param. The trial balance table drives a slide-over
journal-entries panel (the `DrillPanel` recipe); the balance sheet is a pure
client-side derivation over the same trial-balance lines. Close, reopen, and
post-this-period live on the selected period's detail card.

**Tech Stack:** React 19, TypeScript, Vite, TanStack Router + Query,
Tailwind, Vitest + Testing Library. Frontend dir: `frontend/`. Backend:
Python 3.12, FastAPI, pydantic.

Implements §6 of
[`docs/design/2026-09-06-oh27-gl-posting-core-design.md`](../design/2026-09-06-oh27-gl-posting-core-design.md)
(read §2's execution amendments and §11's residuals — they supersede the
original text). The backend shipped as **PR #123**. The SOS cutover
(D-OH27.9) is **not** this plan — it stays gated on `tests/test_gl_parity.py`
and gets its own slice. No chart-editing UI either, per §6: the API exists,
the page waits for a real need. Branch: `feat/oh27-gl-frontend`.

---

## The three things this plan exists to get right

**1. The balance sheet is the period's NET CHANGE, and says so.** The only
read the backend ships is `reporting.trial_balance`, which is scoped to one
fiscal period — so a balance sheet derived from it shows the period's
activity, not a cumulative position. Early on (one period posted) the two are
identical; the moment a second period posts they diverge, and a section
titled "Balance sheet" over period-scoped numbers would be a false statement
of financial position. The section is titled **"Balance sheet — net change
for {period}"** with one sentence saying what it is, and Task 5 pins the
wording. A true as-of balance sheet needs a cumulative trial-balance query —
a backend follow-up, recorded under Deferred, not smuggled in here.

**2. The unposted gap is PMS-only, and no copy may imply otherwise.**
`PeriodModel.unposted_dates` means "financial-fact dates with no current
posted `pms_daily` entry" — pms_daily-only **by design** (its docstring,
`gl_api.py:85-100`: a labor day with no accrual is the ordinary
no-chart/no-cost case, not a gap). A rail that renders "0 unposted days —
all caught up" would claim the payroll side is verified when it was never
checked. Every surface that shows this list labels it as PMS days
("{n} PMS days unposted"), and the period detail card carries the
qualifying sentence. `orphaned_dates` covers both sources and is labeled
without the qualifier. Tasks 4 and 6 each pin the copy.

**3. Amount arithmetic is BigInt fixed-point, never `Number()`.** Amounts
arrive as exact decimal strings (`str(Decimal)`, the `gl_api.py` docstring)
and stay strings. Display goes through `fmtMoney`; any arithmetic — balance
sheet sums, the balanced badge — goes through `lib/decimal.ts`
(`sumFixed`/`eqFixed`, plus a new `subFixed` this plan adds). `eqFixed`
exists precisely because scales differ (`410.00` == `410.0000` — its
docstring); comparing `total_debits` to `total_credits` any other way is
wrong the first time the two serialize at different scales.

## Gates (run for EVERY task before committing)

Frontend tasks, from `frontend/`:

```bash
npm run lint          # oxlint
npm run test          # vitest run
npm run build         # tsc -b && vite build (type-check + bundle)
```

Task 1 is a backend task; from the repo root:

```bash
uv run pytest -q tests/test_gl_api.py tests/test_gl_reporting.py
uv run mypy src
uv run ruff check src tests
```

## Grounding facts (verified against the code — do not re-guess)

### Backend

- **Wire contract, as shipped** (`src/usali/gl_api.py`): `GET
  /api/gl/accounts`; `PUT /api/gl/accounts/{code}`; `GET
  /api/gl/periods?property=&fiscal_year=` → `list[PeriodModel]`
  (`{period_key, state, unposted_dates, orphaned_dates}`); `PUT
  /api/gl/periods/{key}/close` → `CloseResponse` (same four fields); `PUT
  /api/gl/periods/{key}/reopen` (body `{reason}`, 204); `POST /api/gl/post`
  (body `{property_id, date_from, date_to}`, 400-day cap) →
  `list[PostOutcomeModel]`; `GET /api/gl/trial-balance?property=&period=` →
  `TrialBalanceModel` (`{property_id, period_key, date_from, date_to,
  lines[], total_debits, total_credits}`, lines
  `{account_code, name, account_type, debits, credits}`). Decimals
  serialize as `str(Decimal)` — exact through JSON.
- **Auth split** (`gl_api.py:1-12,29`): reads ride the mount's operator
  gates (`server.py:450`); mutations narrow to `require_gl_admin =
  require_grants(ORG_ADMIN)` — the `integrations_api` precedent.
- **Error mapping** (`gl_api.py._run`, :40-56): `NoFactsError` → 404,
  `FiscalCalendarNotConfigured` → 422, other `ValueError` → 422, all with
  the bare message as `detail`.
- **`trial_balance` raises `NoFactsError` on an empty period**
  (`reporting.py:1874-1902`) — so a period with nothing posted is a 404
  with "no journal lines for property X in YYYY-PNN", not an empty report.
  Reversals net out arithmetically in the totals; lines sort by account
  code.
- **`gl_posting.PostOutcome` already carries `entry_id`**
  (`gl_posting.py:417-421`); only the API's `PostOutcomeModel` omits it.
  `OutcomeStatus = Literal["posted", "noop", "reposted", "reversed",
  "skipped", "failed"]` (`gl_posting.py:414`), and `PostOutcomeModel`
  annotates with that Literal so the set cannot drift.
- **`get_periods` discards period bounds** (`gl_api.py:266`): `for key,
  _start, _end in fiscal.periods_in_year(cfg, year)`. The bounds exist at
  the loop already; Task 1 puts them on the wire.
- **Journal shape** (`models.py:360-437`): `JournalEntry {entry_id,
  property_id, business_date, source_type, source_hash, memo, reversal_of,
  posted_by, posted_at}`; `JournalLine {line_id, entry_id, account_code,
  posting ∈ Debit|Credit, amount Numeric(15,4) positive, memo, fact_id
  nullable}`. `fact_id` is the drill link for `pms_daily`;
  `payroll_accrual` lines carry NULL and name their department in `memo`
  (the `JournalLine` docstring). `ix_journal_line_org_entry` exists for
  exactly this read.
- **The fact→stage drill precedent**: `reporting.line_transactions`
  (`reporting.py:1343-1394`) joins `usali_financial_fact` to
  `pms_daily_financial_stage` and returns `StagedTxn {stage_id, pms_source,
  business_date, pms_trx_code, pms_trx_desc, amount, source_file}`; "an
  unknown line simply returns an empty list: drill-through of nothing is
  nothing, not an error." Served by `portal_api.sos_line_transactions`
  (`portal_api.py:1119`).
- **Test fixtures** (`tests/test_gl_api.py:51-86`): `gl_client` (org_admin)
  / `gl_client_gm` / `gl_client_employee`, and `gl_world` — seeded chart +
  calendar over the six-PDF facts, committed because the API reads on
  another connection. `_post_range` helper posts a range and asserts 200.
  Reuse all of them.

### Frontend

- **Routing** (`frontend/src/router.tsx`): `createRoute({getParentRoute: ()
  => rootRoute, path, component, validateSearch?})`, collected in
  `childRoutes` (:263-285) → `rootRoute.addChildren` (:287). `servedPaths`
  derives from the same array, so a new route auto-registers with
  `isServedPath` (and `router.test.ts` pins it). **`validatePropertyMonth`
  (:136-142) is the search-validator precedent**: a regex-clamped optional
  string that falls back to "not picked" rather than firing a doomed
  request. Pages import their search via `getRouteApi('/path')` to avoid
  the router↔page circular import (`SosPage.tsx:26`).
- **Nav** (`frontend/src/Layout.tsx:122-136`): the `Accounting` section
  holds `/sos`, `/upload`, `/reports`, `/performance`, `/qbo`,
  `/integrations` (`show: isOrgAdmin`), `/coverage`, and three `soon`
  placeholders — one of which is **`Financial Reports` with `BankIcon`**
  (:132-134), the slot /gl fills. `soon` items render an inert span;
  live items a `<Link>`. Visibility filter + empty-section collapse at
  :292-293.
- **Property selection**: `useGlobalProperty()`
  (`lib/propertyContext.ts:46-52`) — global select in the top bar
  (`aria-label="Active property"`, `Layout.tsx:406-424`), falls back to
  page-local state in tests. The no-property empty state is
  `PerformancePage.tsx:127-130`: a `Card` with "No property selected yet."
- **No fiscal-period picker exists anywhere yet.** `FiscalPeriod {key,
  start, end}` (`api/types.ts:975-979`) and `getFiscalPeriods`
  (`client.ts:775-782`) exist but feed only a read-only preview list on
  PropertyConfigPage. The segmented-control precedent for a rail:
  `PayrollDashboardPage.tsx:799` (`role="group" aria-label="Reporting
  period"`).
- **API client** (`frontend/src/api/client.ts`): `authHeaders` (:87-94),
  `redirectToLogin` (:102-107, single `redirecting` latch), `raiseApiError`
  (:128-142) are **exported**; `getJson` is private. Writes are hand-rolled
  fetches; 204 writes never call `res.json()`. **`src/api/checklist.ts` is
  the per-feature-module precedent** — imports the three seam functions,
  spells out its own GET. A new `src/api/gl.ts` follows it exactly.
  `types.ts` organizes mirrors in banner-comment sections naming the
  backend models field-for-field.
- **Drill-through precedent**: `components/Statement.tsx` — every financial
  line is a `<button>` handing its line to `onLineClick` (:70,
  `lineButtonClass` :22-23); `components/DrillPanel.tsx` — right slide-over,
  `role="dialog" aria-modal aria-label`, focuses Close on mount (:36-38),
  closes on Escape (:39-45); the page owns the fetch with `skipToken`
  (`SosPage.tsx:79-91`) and clears drill state when the property changes
  (`SosPage.tsx:39`).
- **In-page org_admin gate precedent**: `ChecklistPage.tsx:40-43` — `const
  canDismiss = hasRole(me.data, 'org_admin')` with the comment "Mirrors the
  endpoint's own gate… Offering the control to anyone else would only
  produce a 403 on click." `hasRole` (`lib/roles.ts:9-12`): a missing
  `me` is never privileged.
- **Destructive-action confirm precedent**: `IntegrationsPage.tsx:115-180`
  (`ConnectedActions`) — a same-card confirm branch on local state, never
  `window.confirm`.
- **UI primitives** (`components/ui.tsx`): `Card` (spreads rest props, so
  `role="region" aria-label` works), `PageHeader {title, subtitle?,
  actions?, level?}`, `Badge` (tones `ok|warn|danger|info|neutral`; the
  color words in the class strings are asserted on by tests), and the
  hand-rolled-table class constants `tableClass`, `headCellClass`,
  `amountHeadClass`, `cellClass`, `amountCellClass` (right-aligned
  `tabular-nums`). `TxnTable` in `DrillPanel.tsx:89-121` is the model
  table, including the BigInt footer sum.
- **Money** (`lib/format.ts`): `fmtMoney(s)` — display only, no currency
  symbol. **Arithmetic** (`lib/decimal.ts`): `addFixed`, `eqFixed`
  ("exact numeric equality across differing scales"), `sumFixed`. There is
  **no `subFixed`** — Task 5 adds it. `DrillPanel.tsx:86-87` is the model:
  `sumFixed` then `eqFixed` then a `reconciled ✓` / danger badge.
- **Errors**: `errorMessage(err)` (`lib/errors.ts:7-10`) renders an
  `ApiError`'s bare `detail`; the failure line shape is
  `SosPage.tsx:125-129`.
- **Page-test convention**: `SosPage.test.tsx` — `vi.mock('../api/client',
  async (importOriginal) => ({...spread, fns: vi.fn()}))` (the spread keeps
  the real `ApiError` class for `instanceof`), render the **real router**
  via `createAppRouter(createMemoryHistory({initialEntries: ['/gl']}))`
  inside `QueryClientProvider` + `AuthContext.Provider value={AUTHED_CONTEXT}`
  (`test/fixtures.ts:21-25`). A full mock (no importOriginal) is fine for
  modules with no class exports — `App.test.tsx:17-21` does it for
  `./api/checklist`.
- **jsdom gotchas** (encode, don't rediscover): role-gating tests must wait
  on `queryClient.getQueryData(['me'])` before asserting absence — waiting
  on an ungated element is vacuous (`App.test.tsx:129-134,149-153`).
  Prefer a visible `<label htmlFor>` over `aria-label` for form controls —
  jsdom's accname diverges from the browser's on sr-only/aria-label text
  (`Layout.tsx:179-203` records the incident). Buttons repeated per row
  carry the row's identity in their accessible name (`ChecklistPage`'s
  `Dismiss ${title}` precedent).
- **CI runs `npx tsc --noEmit`, `npx oxlint`, `npm test`** from `frontend/`
  (`.github/workflows/ci.yml:61-67`); `npm run build` locally covers the
  type-check.

## File structure

```
MOD  src/usali/reporting.py                    # Task 1 — journal_entries drill query
MOD  src/usali/gl_api.py                       # Task 1 — GET /entries; entry_id; period bounds
MOD  tests/test_gl_reporting.py                # Task 1
MOD  tests/test_gl_api.py                      # Task 1

MOD  frontend/src/api/types.ts                 # Task 2 — GL mirrors
NEW  frontend/src/api/gl.ts                    # Task 2 — the six calls
NEW  frontend/src/api/gl.test.ts               # Task 2
MOD  frontend/src/test/fixtures.ts             # Task 2 — makeGlPeriod / makeTrialBalance / makeJournalEntry

MOD  frontend/src/router.tsx                   # Task 3 — /gl + ?period= validator
NEW  frontend/src/pages/GlPage.tsx             # Tasks 3-7
NEW  frontend/src/pages/GlPage.test.tsx        # Tasks 3-7
NEW  frontend/src/components/JournalDrillPanel.tsx  # Task 4
MOD  frontend/src/lib/decimal.ts               # Task 5 — subFixed
NEW  frontend/src/lib/balanceSheet.ts          # Task 5 — pure derivation
NEW  frontend/src/lib/balanceSheet.test.ts     # Task 5

MOD  frontend/src/Layout.tsx                   # Task 8 — nav entry
MOD  frontend/src/App.test.tsx                 # Task 8
MOD  docs/ROADMAP.md  .github/roadmap.yml      # Task 9
```

### Where this diverged from what shipped

Recorded rather than retrofitted, following the B4 plan's practice.

| Planned | Shipped | Why |
|---|---|---|
| Task 2's `makeJournalEntry` with "one pms line and one payroll line" | a balanced pms_daily entry — Credit 4100 with full provenance, Debit 1210 with nulls | a payroll line inside a pms_daily entry is a shape the backend never emits; which lines carry provenance is `gl_posting.JeLine`'s multi-fact-group rule |
| Task 2's test list | plus a single 401 test (the `checklist.test.ts` latch pattern — one per file, never two) | without it the `redirectToLogin` wiring in all six functions was uncovered |
| Task 3's stepper "defaults to the backend's default (the year containing today)" | the client's calendar year, seeded from a deep-linked `?period=`'s first four digits, always passed explicitly | the query key names the year actually fetched; the comment above `fiscalYear` in `GlPage.tsx` records the divergence from `gl_api.get_periods`' fiscal-year default |
| Tasks 4–7 "in `GlPage.tsx`" | `TrialBalanceCard`, `BalanceSheetCard`, `PeriodDetailCard` extracted to `components/` in pure-move commits before the features that grew them | the page was passing 400 lines before the mutations moved in |
| Task 4's dialog name "Rooms revenue" | "Rooms Revenue" | the fixture is the authority for the account's name |
| `subFixed` tests sketched in `balanceSheet.test.ts` | moved to `decimal.test.ts` beside `addFixed`/`eqFixed` | coverage lives where an auditor of `decimal.ts` will look |
| Task 6: "render close's response while the refetch lands" | the response is written into the periods cache (`setQueryData`), then invalidated | a held response never yielded back to the query; the cache is the one truth the card reads |
| Task 6's pinned confirm regex (`?` directly after "days") | the both-gaps copy joins `and {n} orphaned entries`; the close-flow test uses an orphan-free period; the gap heading pre-introduces "(orphaned)" | the regex cannot match the both-gaps string, and a term should not debut inside a destructive confirm |
| Task 7's outcomes table | labeled "Post outcomes"; `post.error` renders inside the open-state controls | an unlabeled red line was outliving the verb it belonged to |
| Task 8: "`router.test.ts` already pins served paths from the same array" | it pinned only the four checklist routes; an explicit `isServedPath('/gl')` pin was added — not to `CHECKLIST_ROUTES`, which is closed and twinned with `test_every_item_route_is_pinned` | the registration is automatic; the plan overstated the test coverage it inherits |

---

## Task 1 — the drill-through endpoint, `entry_id`, and period bounds

**Files:** Modify `src/usali/reporting.py`, `src/usali/gl_api.py`,
`tests/test_gl_reporting.py`, `tests/test_gl_api.py`.

The three §11 deltas, all additive; no migration, no engine change.

**Endpoint shape (decided here):** `GET
/api/gl/entries?property=&period=&account=` returns every journal entry in
the period with **at least one line on that account**, each with **all** its
lines — the operator drills to see the double-entry context, not half of it.
Lines that carry a `fact_id` are joined through to their staged PMS
transaction inline (`pms_trx_code`, `pms_trx_desc`, `source_file` — the
`StagedTxn` fields), so "line → source fact" is one fetch, no N+1 and no
second endpoint. `payroll_accrual` lines have no fact by design; their
department lives in `memo` and the three txn fields are null. Empty result →
empty list, the `line_transactions` precedent ("drill-through of nothing is
nothing, not an error"). Ordered `(business_date, entry_id, line_id)` so
re-reads are stable and a reversal renders beside what it reverses.

- [ ] **Step 1: failing tests** — add to `tests/test_gl_reporting.py` (reuse
  its existing posted-world fixture; **resolve at implementation:** copy the
  seeding the file's `trial_balance` tests already do, do not invent a new
  world):

```python
def test_journal_entries_drill_returns_whole_entries():
    """Filter by account, return whole entries: every entry in the result
    has >=1 line on the asked account AND carries all its lines — the
    double-entry context. Amounts arrive as Decimal; ordering is
    (business_date, entry_id, line_id)."""

def test_journal_entries_drill_joins_the_staged_txn():
    """A pms_daily line with a fact_id carries pms_trx_code / pms_trx_desc /
    source_file from its fact's stage row; a payroll_accrual line (fact_id
    None) carries None for all three and its department memo."""

def test_journal_entries_drill_includes_reversals():
    """After a reverse-and-repost, drilling the account returns the original,
    the reversal (reversal_of set), and the correction — the audit trail the
    trial balance's nets summarize."""

def test_journal_entries_drill_of_nothing_is_nothing():
    """An account with no lines in the period -> [] (the line_transactions
    precedent), never an error."""
```

  and to `tests/test_gl_api.py`:

```python
def test_entries_endpoint_mirrors_reporting(gl_client, gl_world, db_session):
    """GET /api/gl/entries mirrors reporting.journal_entries field-for-field,
    amounts as str(Decimal) — the trial-balance test's shape. Drill an
    account taken from the trial-balance response so the join key the page
    will use (TrialBalanceLineModel.account_code) is the one exercised."""

def test_post_outcomes_carry_the_entry_id(gl_client, gl_world):
    """Every 'posted' outcome names its entry_id, and that id appears in the
    entries drill for the same period — the wire-level link the page uses."""

def test_periods_carry_their_date_bounds(gl_client, gl_world):
    """PeriodModel gains date_from/date_to from periods_in_year's own tuple —
    the same source resolve_period uses, so the rail and the post range can
    never disagree with the backend's period arithmetic."""
```

- [ ] **Step 2: run RED** — `uv run pytest -q tests/test_gl_reporting.py tests/test_gl_api.py`.
  Expected: `AttributeError` on `journal_entries`, missing-field failures on
  the models.

- [ ] **Step 3: implement.** In `reporting.py`, next to `trial_balance`:

```python
@dataclass(frozen=True)
class JournalLineDetail:
    line_id: int
    account_code: str
    account_name: str
    posting: str
    amount: Decimal
    memo: str | None
    fact_id: int | None
    pms_trx_code: str | None
    pms_trx_desc: str | None
    source_file: str | None


@dataclass(frozen=True)
class JournalEntryDetail:
    entry_id: int
    business_date: date
    source_type: str
    memo: str | None
    reversal_of: int | None
    posted_by: str
    posted_at: datetime
    lines: list[JournalLineDetail]


def journal_entries(
    session: Session, *, property_id: str, period_key: str, account_code: str
) -> list[JournalEntryDetail]:
```

  Resolve the period via `fiscal.require_config` + `fiscal.resolve_period`
  (inherits `FiscalCalendarNotConfigured`, which `_run` already maps to
  422). Select entry ids having a line on `account_code` in the window
  (a subquery), then the entries with lines joined to `GlAccount` (name) and
  outer-joined `fact → stage` for the three txn fields. One round trip per
  drill; `ix_journal_line_org_entry` is the index the per-entry line read
  rides.

  In `gl_api.py`: `JournalLineModel` / `JournalEntryModel` /
  `JournalEntriesModel {property_id, period_key, account_code, entries}`
  (amounts as `str(Decimal)`, `extra="forbid"` like every model in the
  file); the GET with `property`/`period`/`account` query aliases through
  `_run`. `PostOutcomeModel` gains `entry_id: int | None`. `PeriodModel`
  gains `date_from: date` / `date_to: date`, filled from the tuple
  `get_periods` already iterates (`gl_api.py:266`). `CloseResponse` stays as
  it is — the close's answer is state and gaps, not calendar arithmetic.

- [ ] **Step 4: run GREEN** — the two files, then the wider GL set:
  `uv run pytest -q tests/test_gl_api.py tests/test_gl_reporting.py tests/test_gl_posting.py tests/test_gl_periods.py`
- [ ] **Step 5: gates + commit**

```bash
uv run mypy src && uv run ruff check src tests
git add -A && git commit -m "feat(oh27): the journal answers for its lines — drill-through on the wire"
```

---

## Task 2 — `src/api/gl.ts` + the type mirrors

**Files:** Create `frontend/src/api/gl.ts`, `frontend/src/api/gl.test.ts`.
Modify `frontend/src/api/types.ts`, `frontend/src/test/fixtures.ts`.

The `checklist.ts` precedent exactly: own module, imports `authHeaders` /
`raiseApiError` / `redirectToLogin` from `./client`, spells out its own GET.

- [ ] **Step 1: failing tests** — `frontend/src/api/gl.test.ts`, the
  `checklist.test.ts` shape (mock `../auth/oidc`, spy on `fetch`):

  - `getGlPeriods('HISJ')` GETs `/api/gl/periods?property=HISJ`;
    `getGlPeriods('HISJ', 2026)` appends `&fiscal_year=2026`.
  - `getTrialBalance('HISJ', '2026-P07')` GETs
    `/api/gl/trial-balance?property=HISJ&period=2026-P07` and returns the
    parsed body.
  - `getJournalEntries('HISJ', '2026-P07', '4100')` GETs
    `/api/gl/entries?property=HISJ&period=2026-P07&account=4100`.
  - `closeGlPeriod('HISJ', '2026-P07')` PUTs
    `/api/gl/periods/2026-P07/close?property=HISJ` and returns the
    `CloseResponse` body.
  - `reopenGlPeriod('HISJ', '2026-P07', 'because')` PUTs `{reason:
    'because'}` and resolves `undefined` on a bodyless 204.
  - `postGlRange({property_id, date_from, date_to})` POSTs `/api/gl/post`
    and returns the outcomes array.
  - a 422 close surfaces its `detail` (`rejects.toThrow`).

- [ ] **Step 2: run RED** — `npm run test -- gl`

- [ ] **Step 3: implement.** In `types.ts`, a new banner section:

```ts
// --- General ledger (OH-27) --------------------------------------------------
// Mirrors PeriodModel/CloseResponse/TrialBalance*/JournalEntriesModel/
// PostOutcomeModel in src/usali/gl_api.py field-for-field. Every amount is an
// exact decimal string (str(Decimal)) — never parse to float.
```

  with `GlPeriod {period_key, state: 'open'|'closed', date_from, date_to,
  unposted_dates: string[], orphaned_dates: string[]}`, `GlCloseResponse`,
  `TrialBalanceLine`, `TrialBalance`, `JournalLine` (nullable
  `fact_id`/`pms_trx_code`/`pms_trx_desc`/`source_file`), `JournalEntry`
  (`reversal_of: number | null`), `JournalEntries`, `GlPostOutcome` with
  `status: 'posted'|'noop'|'reposted'|'reversed'|'skipped'|'failed'` and
  `entry_id: number | null`.

  Then `src/api/gl.ts` with the six functions; the two 204 writes never
  touch `res.json()`. Fixtures: `makeGlPeriod()`, `makeTrialBalance()`
  (a small balanced chart: cash 1010, guest-ledger 1210, revenue 4100,
  wages 6100 — debits==credits so the balanced badge is honest), and
  `makeJournalEntry()` with one pms line (txn fields set) and one payroll
  line (nulls, department memo), dated inside `HISJ_PROPERTY`'s
  `2026-07-01..07` window so property bounds and fixtures agree.

- [ ] **Step 4: run GREEN** — `npm run test -- gl`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(oh27-spa): typed client for the /api/gl surface"
```

---

## Task 3 — `/gl`: route, period rail, trial balance

**Files:** Create `frontend/src/pages/GlPage.tsx`,
`frontend/src/pages/GlPage.test.tsx`. Modify `frontend/src/router.tsx`.

The rail IS the picker: one row of period chips (a `role="group"
aria-label="Fiscal periods"`, the PayrollDashboard segmented-control
precedent), each showing its key and a `Badge` for `closed`. Selecting a
chip navigates to `?period=KEY`, so the selection survives reload and is
shareable — the `validatePropertyMonth` pattern with
`PERIOD_RE = /^\d{4}-P\d{2}$/`. A `Fiscal year` stepper (visible `<label
htmlFor>`, per the jsdom accname incident) refetches the rail for other
years; it defaults to the backend's default (the year containing today).

- [ ] **Step 1: failing tests** — `GlPage.test.tsx`, the `SosPage.test.tsx`
  harness (real router at `/gl`; **resolve at implementation:** mirror
  whatever `SosPage.test.tsx` mocks — `../api/client` with importOriginal
  spread for `getProperties`/`getMe`, and `../api/gl` as a full mock; if the
  layout's checklist query needs stubbing here, copy `App.test.tsx`'s
  treatment):

  - "No property selected yet." renders when there is no property
    (Performance precedent).
  - the rail renders a chip per period from `getGlPeriods`, closed ones
    carrying a `Closed` badge.
  - clicking a chip fetches the trial balance for that period and renders
    the table: account code, name, type, debits, credits via `fmtMoney`,
    a totals row, and a `balanced ✓` ok-badge when `eqFixed(total_debits,
    total_credits)` — fixture serializes the two at different scales
    (`"100.00"` vs `"100.0000"`) so string equality would fail and only
    `eqFixed` passes.
  - an empty period (mock `getTrialBalance` rejecting with
    `new ApiError(404, 'no journal lines for property HISJ in 2026-P08')`)
    renders the detail as a quiet empty state, not a red failure — 404 is
    the backend's word for "nothing posted here", the one state every new
    tenant starts in.
  - a real failure (503) renders the loud
    `Failed to load trial balance: …` line (`errorMessage`).
  - `?period=2026-P07` in the initial URL preselects that chip; a malformed
    `?period=garbage` renders as no selection (the validator clamped it).

- [ ] **Step 2: run RED** — `npm run test -- GlPage`

- [ ] **Step 3: implement.**

  `router.tsx`: `GlSearch = {period?: string}`, validator clamping via
  `PERIOD_RE`; `glRoute` at `/gl` added to `childRoutes` (servedPaths and
  `router.test.ts` pick it up from the array). `GlPage` reads it via
  `getRouteApi('/gl')` and writes it with `navigate({search: …})`.

  `GlPage.tsx`: `PageHeader` ("General Ledger" / "The trial balance and the
  books' periods, from the journal."). `useGlobalProperty()`; periods query
  `['gl-periods', property, fiscalYear]` with `skipToken` when no property;
  trial balance `['gl-trial-balance', property, period]`, `skipToken` until
  both exist. Clear drill state on property change (the `SosPage.tsx:39`
  effect). Table uses the `ui.tsx` class constants (`amountCellClass` for
  the money columns); totals row `border-t border-line-strong font-semibold`
  (the `TxnTable` recipe). The 404-vs-failure split branches on
  `err instanceof ApiError && err.status === 404`.

- [ ] **Step 4: run GREEN** — `npm run test -- GlPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(oh27-spa): /gl — the trial balance behind a period rail"
```

---

## Task 4 — drill-through: a trial-balance line opens its journal entries

**Files:** Create `frontend/src/components/JournalDrillPanel.tsx`. Modify
`frontend/src/pages/GlPage.tsx`, `frontend/src/pages/GlPage.test.tsx`.

The `Statement` → `DrillPanel` pair, retold for the journal: every
trial-balance account row's name is a `<button>` (`lineButtonClass`) handing
its `account_code` up; the page fetches `['gl-entries', property, period,
account]` and renders a right slide-over.

- [ ] **Step 1: failing tests** — add to `GlPage.test.tsx`:

  - clicking an account row opens a dialog named `Journal entries: 4100 —
    Rooms revenue` listing each entry: date, source, `entry #12`,
    `posted by`, and its lines table with Debit/Credit amounts.
  - a reversal entry shows a `reversal of entry #12` badge
    (`reversal_of` set — the fixture from Task 2).
  - a pms line shows its staged transaction (`pms_trx_code` +
    `source_file`); a payroll line shows its department memo and **no**
    transaction placeholder — absent means "no source transaction by
    design" (accrual lines have no fact), and the panel must not render it
    as missing data.
  - Escape closes the panel; focus lands on Close when it opens (the
    `DrillPanel.tsx:36-45` recipe — assert both, they are the a11y
    contract).
  - drilling an account with entries but the request failing renders the
    loud failure line inside the panel.

- [ ] **Step 2: run RED** — `npm run test -- GlPage`

- [ ] **Step 3: implement.** `JournalDrillPanel` copies `DrillPanel`'s
  dialog shell (focus-on-mount, Escape, scrim) but takes
  `{property, period, account, accountName, entries, error, onClose}` —
  the page owns the fetch, the panel renders. Per entry: a header row
  (business date, `source_type`, `entry #{entry_id}`, `posted_by`,
  memo, and the reversal badge `tone="info"` when `reversal_of !== null`),
  then a lines table: account, memo, Debit, Credit (amount in the column
  its posting names — direction never in a sign, the wire's own
  convention), txn code + source file on pms lines. Amounts via
  `fmtMoney`.

- [ ] **Step 4: run GREEN** — `npm run test -- GlPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(oh27-spa): a trial-balance line opens its journal entries"
```

---

## Task 5 — the balance sheet: a pure derivation, honestly titled

**Files:** Create `frontend/src/lib/balanceSheet.ts`,
`frontend/src/lib/balanceSheet.test.ts`. Modify
`frontend/src/lib/decimal.ts`, `frontend/src/pages/GlPage.tsx`,
`frontend/src/pages/GlPage.test.tsx`.

Client-side from the same trial-balance lines (`account_type` is on the line
model for exactly this). See "things to get right" #1: this is the period's
**net change**, titled as such.

- [ ] **Step 1: failing tests** — `balanceSheet.test.ts` (pure, no DOM):

  - `subFixed('100.00', '30.0000')` → an exact `'70'`-valued string
    (scale-normalized like `addFixed`).
  - `balanceSheet(lines)` groups by `account_type` into
    `{assets, liabilities, equity}` sections of `{account_code, name, net}`
    — assets net `debits − credits`, the other side `credits − debits`,
    `contra_asset` inside assets with its negative net — plus a synthetic
    equity line `Net income (this period)` = income nets − expense nets.
  - the foot: with a balanced input, `eqFixed(totalAssets,
    totalLiabilitiesAndEquity)` holds; with a deliberately unbalanced input
    it does not (the function reports, never throws — the DB trigger owns
    that wall, `test_gl_wall.py`).
  - zero-net accounts (a fully reversed account) are dropped from the
    sections — a statement line of 0.00 is noise, and the drill for the
    why lives on the trial balance above it.

  and in `GlPage.test.tsx`: the section renders under the heading
  **`Balance sheet — net change for 2026-P07`** with the qualifier sentence
  (`/activity for this period, not a cumulative position/i`), and its
  own balanced badge.

- [ ] **Step 2: run RED** — `npm run test -- balanceSheet GlPage`

- [ ] **Step 3: implement.** `subFixed` beside `addFixed` in `decimal.ts`
  (same BigInt scale-normalization; **resolve at implementation:** reuse
  the module's existing normalization helper rather than re-deriving it).
  `balanceSheet.ts` exports the derivation and its section/foot types;
  `GlPage` renders it below the trial balance from the **same query data**
  — no second fetch, so the two can never disagree. Title and qualifier
  exactly as pinned.

- [ ] **Step 4: run GREEN** — `npm run test -- balanceSheet GlPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(oh27-spa): the balance sheet says what it is — net change, derived in place"
```

---

## Task 6 — close and reopen, org_admin-gated, gaps as visible choices

**Files:** Modify `frontend/src/pages/GlPage.tsx`,
`frontend/src/pages/GlPage.test.tsx`.

The selected period's detail card: state, both gap lists with the honest
copy of "things to get right" #2, and the two controls. Close confirms
in-place naming the gaps (the `ConnectedActions` precedent — never
`window.confirm`, which also blocks the browser-automation harness); reopen
requires a reason.

- [ ] **Step 1: failing tests** — add to `GlPage.test.tsx` (org_admin comes
  from mocking `getMe`; the absence assertions wait on
  `queryClient.getQueryData(['me'])` first — the vacuous-test trap in
  `App.test.tsx:129-134`):

  - the card shows `3 PMS days unposted` with the qualifier sentence
    (`/payroll accruals are not checked here/i`) and
    `1 posted entry lost its facts` for `orphaned_dates` — and shows the
    dates themselves.
  - `Close 2026-P07` (accessible name carries the key) is offered to an
    org_admin; clicking flips to an in-card confirm that **names the gaps**
    (`/close 2026-P07 with 3 unposted PMS days\?/i`); confirming calls
    `closeGlPeriod` and the card re-renders from the invalidated periods
    query showing `Closed`.
  - a closed period offers `Reopen 2026-P07` with a visible-labeled reason
    field; submit is disabled while the reason is empty; submitting calls
    `reopenGlPeriod('HISJ', '2026-P07', reason)`.
  - a `property_gm` (`getMe` → roles `['property_gm']`) sees state and gaps
    but **no** close/reopen buttons (after the `['me']` wait).
  - a refused close (422) surfaces its detail inline via `errorMessage`.

- [ ] **Step 2: run RED** — `npm run test -- GlPage`

- [ ] **Step 3: implement.** `canManage = hasRole(me.data, 'org_admin')`
  with the ChecklistPage mirror-comment. One `useMutation` per verb, both
  `onSuccess` invalidating `['gl-periods', property, fiscalYear]` (close's
  response is also the freshest gap picture — render it while the refetch
  lands). Reopen's reason is a `<textarea>` with `<label htmlFor>`. Gap
  copy, exactly:

  - unposted: `{n} PMS days unposted` + "Days with promoted PMS facts and
    no posted journal entry. Payroll accruals are not checked here — a day
    with no accrual is the ordinary no-cost case."
  - orphaned: `{n} posted entries lost their facts` + "Days whose entry's
    source facts were re-transformed away — both sources. Re-posting
    reverses them." (what `status: reversed` in Task 7 then shows
    happening.)

- [ ] **Step 4: run GREEN** — `npm run test -- GlPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(oh27-spa): close and reopen — gaps named, reasons required"
```

---

## Task 7 — post this period

**Files:** Modify `frontend/src/pages/GlPage.tsx`,
`frontend/src/pages/GlPage.test.tsx`.

The remedy the gap lists point at, in the same card: an org_admin posts the
selected period's range (`date_from`/`date_to` from `GlPeriod` — the Task 1
bounds) and reads per-grain outcomes. This is `POST /api/gl/post` — the
period is at most ~31 days, far under the endpoint's 400-day CLI cutoff.
Only offered on an **open** period; posting into a closed one is the
backend's refusal, and a button that can only 422 is the dishonesty the
checklist page refused first.

- [ ] **Step 1: failing tests** — add to `GlPage.test.tsx`:

  - `Post 2026-P07` is offered to an org_admin on an open period; not on a
    closed one; not to a `property_gm` (with the `['me']` wait).
  - clicking calls `postGlRange({property_id: 'HISJ', date_from:
    '2026-07-01', date_to: '2026-07-31'})` and renders the outcomes table:
    one row per (date, source) with the status word, the message when
    present, and `entry #N` when `entry_id` is set.
  - status tones: `posted`/`reposted` → ok, `noop` → neutral, `reversed` →
    warn, `skipped` → neutral, `failed` → danger — and a `failed` row's
    message is visible without interaction.
  - a successful post invalidates: the trial balance and periods queries
    refetch (assert the mocked getters are called again).

- [ ] **Step 2: run RED** — `npm run test -- GlPage`

- [ ] **Step 3: implement.** A `useMutation` over `postGlRange`;
  `onSuccess` invalidates `['gl-periods', …]`, `['gl-trial-balance', …]`
  and `['gl-entries', …]` (prefix invalidation on the family keys).
  Outcomes render in a plain `tableClass` table below the controls;
  `skipped` is neutral, not a warning — the backend uses it for the honest
  "no chart yet / nothing to post" answer (`test_post_without_a_chart…`),
  and painting it amber would nag every tenant that has not seeded a chart.
  While the mutation is pending, disable the button (a 31-day post runs the
  engine 62 times).

- [ ] **Step 4: run GREEN** — `npm run test -- GlPage`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(oh27-spa): post the period from where its gaps are named"
```

---

## Task 8 — the nav entry

**Files:** Modify `frontend/src/Layout.tsx`, `frontend/src/App.test.tsx`.

The `Financial Reports` soon-placeholder (`Layout.tsx:132-134`, `BankIcon`)
retires; the live entry takes its slot in the Accounting section. No `show`
gate: reads ride the mount's operator gates, so every operator who can see
the sidebar may see the books — the close/post controls inside are
separately gated (Tasks 6-7), the same split `/setup` uses.

- [ ] **Step 1: failing tests** — add to `App.test.tsx`:

  - `shows the General Ledger link to any operator` — a plain-operator `me`;
    `findByRole('link', {name: /general ledger/i})` with `href` `/gl`.
  - `the Financial Reports placeholder is gone` — no `Financial Reports`
    text remains (the placeholder's "Soon" pill must not survive beside the
    live entry it promised).

- [ ] **Step 2: run RED** — `npm run test -- App`

- [ ] **Step 3: implement.** In the Accounting section, replace the
  placeholder item with
  `{ to: '/gl', label: 'General Ledger', icon: BankIcon }`. Nothing else:
  the route registered with `servedPaths` in Task 3, and `router.test.ts`
  already pins served paths from the same array.

- [ ] **Step 4: run GREEN** — `npm run test -- App`
- [ ] **Step 5: gates + commit**

```bash
npm run lint && npm run test && npm run build
git add -A && git commit -m "feat(oh27-spa): the books get a door — General Ledger in the nav"
```

---

## Task 9 — roadmap status

**Files:** Modify `docs/ROADMAP.md`, `.github/roadmap.yml`.

- [ ] In ROADMAP §3 Tier 0 #3 (line ~158), reword the annotation: backend
  **and /gl page** shipped; what remains of OH-27 is the SOS cutover, still
  gated on `tests/test_gl_parity.py` running green over real data.
- [ ] `.github/roadmap.yml` OH-27 stays `in-progress` — the catalogue entry
  promises the re-pointed operating statement, and that promise is not yet
  kept. Flipping it to done here would be the silent-widening drift §1 was
  written to forbid.
- [ ] Gates (docs-only, but keep the branch green): `npm run lint && npm
  run test && npm run build` from `frontend/`, then commit.

---

## Self-review checklist

- [ ] **No `Number()` arithmetic on amounts.** `grep -rn "Number("
  frontend/src/pages/GlPage.tsx frontend/src/lib/balanceSheet.ts` returns
  nothing; every sum/diff/comparison goes through `lib/decimal.ts`, every
  display through `fmtMoney`.
- [ ] The balanced badges (trial balance, balance sheet) use `eqFixed`, and
  at least one fixture serializes the compared values at different scales.
- [ ] Every rendering of `unposted_dates` says **PMS** in the visible copy,
  and the qualifier sentence about payroll appears on the period card. No
  copy anywhere reads "everything is posted" or "all caught up".
- [ ] The balance-sheet heading contains "net change" and the qualifier
  sentence; nothing on the page calls period activity a financial position.
- [ ] Close/reopen/post render only for org_admin (mirror comments in
  place), their accessible names carry the period key, and every absence
  test waits on `['me']` before asserting.
- [ ] Reopen cannot submit an empty reason client-side, AND a server 422
  still surfaces via `errorMessage` — the button gate is convenience, the
  API is the wall.
- [ ] The drill panel focuses Close on open and closes on Escape; its
  `aria-label` names the account.
- [ ] A 404 trial balance renders as an empty state; only non-404 failures
  render the red failure line.
- [ ] `?period=` is regex-clamped; a malformed value never fires a request.
- [ ] Mutations invalidate the family keys; no surface holds a stale period
  state after close/reopen/post.
- [ ] Every task ran its gates green before committing (Task 1: pytest +
  mypy + ruff; frontend tasks: lint + test + build).

## Deferred / follow-ups

- **A true as-of balance sheet.** Needs a cumulative trial-balance read
  (journal from inception to a date) backend-side; the page then swaps its
  derivation input and drops the "net change" qualifier. The client-side
  derivation and its tests survive unchanged.
- **The SOS cutover** — its own slice, per design §9 and §11: the decision
  doc must state the two things the parity gate does not cover (mapping
  coverage; the clearing-lookup convention).
- **Chart-editing UI.** `GET/PUT /api/gl/accounts` shipped and tested;
  design §6 defers the page until a real need.
- **A single-entry permalink / entry-id drill.** `entry_id` is on the wire
  (post outcomes name it, the panel shows it); a `?entry=` deep link is a
  pure frontend addition when someone asks for it.
- **e2e.** Same posture as the checklist plan: the Playwright harness would
  need `scripts/e2e_backend.py` to seed a posted world; the vitest tests
  cover the render logic and `tests/test_gl_api.py` covers the contract.
  Note it, do not force it.
- **Read-path indexes.** `journal_line` has no index by `account_code` and
  `journal_entry` none by `(property_id, business_date)`; both
  `reporting.trial_balance` and `reporting.journal_entries` filter on
  exactly those — sequential scans that are now user-facing on /gl. One
  follow-up migration; it serves the shipped trial balance as much as the
  new drill.
- **A shared slide-over shell.** `DrillPanel` and `JournalDrillPanel` carry
  identical dialog shells with the same two omissions — the document-level
  Escape listener never checks `defaultPrevented`, and focus is not
  restored to the triggering row on close. Fix once in an extracted shell
  on the next touch, not piecemeal.
- **`lineButtonClass` belongs in `ui.tsx`.** Two identical constants
  (`Statement.tsx`, `TrialBalanceCard.tsx`) whose identity is a design
  requirement; today the copy names its original in a comment — one
  direction only — which detects drift only for a reader who greps.
