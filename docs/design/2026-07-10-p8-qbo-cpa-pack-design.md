# USALI Engine — P8 QBO Push + CPA Monthly Pack Design

**Date:** 2026-07-10
**Status:** Approved for planning
**Depends on:** P0–P7 (merged; fact tables, reporting layer, CLI, portal)

## Goal

Post normalized USALI financials into QuickBooks Online as balanced journal entries —
built and fully tested against an in-repo **mock QBO** so no Intuit account is needed —
and produce the **CPA monthly report pack** (Sales Report, Sales Tax/Occupancy, A/R
balances) on both the CLI and the portal. One phase, one merge.

## Decisions locked with the user

1. **One big P8 with mock-QBO** — the mock removes the external dependency; real Intuit
   sandbox/production becomes a config switch later (base URL + credentials only).
2. **JE granularity: one journal entry per property + business date** — matches ingest
   cadence; idempotency key is (property, date); month-end = ~30 small JEs.
3. **A/R included** — extend the Opera Trial Balance adaptor to parse the currently
   skipped ledger section (guest/city/deposit ledger balances). Opera only; Autoclerk's
   Manager Report ledger rows stay guarded noise.
4. **CPA pack surface: CLI + portal pages.**
5. **Approach A for GL mapping & push state** — reuse the existing `gl_account_code`
   plumbing (dictionary column + fact column, both currently NULL everywhere); new
   `qbo_push_ledger` table for idempotency/audit (first schema addition since P5).
6. **Portal gets a full Push button** — a deliberate break from P7's read-only rule,
   confirmed: the QBO page can trigger real pushes (confirm dialog required). The portal
   remains localhost/single-user/no-auth.

## Architecture

```
mapping/opera.yaml + autoclerk.yaml     gl_account_code populated per trx code (placeholder CoA)
        ▼ (seed-mappings, existing)
usali_mapping_dictionary.gl_account_code ── transform (existing) ──► usali_financial_fact.gl_account_code
                                                                            ▼
src/usali/qbo_push.py      NEW: build balanced JE per (property, business_date); push via client;
                           record in qbo_push_ledger; refuse dates with unmapped GL codes
src/usali/qbo_client.py    NEW: httpx client — OAuth2 token + refresh, 429 backoff, RequestId
src/usali/qbo_mock.py      NEW: FastAPI mock QBO (token, journalentry, account query, faults)
        ▼
qbo_push_ledger            NEW TABLE (alembic): property_id, business_date, request_hash,
                           qbo_je_id, status(pushed|failed|stale), pushed_at, payload_summary
```

- **JE shape:** credits = revenue lines grouped by `gl_account_code` (schedules 1–4) and
  taxes-payable (pass-through tax lines). Debits = settlement lines by settlement type
  (cash/card clearing accounts, from the settlements facts, sign-flipped) **plus one
  balancing line to the A/R-clearing account** for the un-settled remainder — that
  remainder IS the day's guest/city-ledger movement (revenue + taxes charged but not yet
  paid). Every JE validates Σdebits == Σcredits exactly (Decimal) before leaving the
  process; a day where the balancing line is zero is normal, a negative remainder
  (payments exceeding charges, e.g. city-ledger paydowns) debits settlements and credits
  A/R-clearing — same line, opposite side.
- **Unmapped GL codes:** any fact in the JE window with `gl_account_code IS NULL` →
  push refuses with the list of offending USALI lines (the coverage report shows the
  same list as a GL worklist — curation happens in YAML, reseed, retry).
- **Idempotency (two layers):** local `qbo_push_ledger` unique on (property, date) —
  re-push of an already-pushed date is a no-op unless facts changed (request_hash
  mismatch → status `stale`, surfaced but NOT auto-voided; amend/void flows are out of
  scope). Remote: QBO `RequestId` parameter (derived from the request_hash) makes the
  POST exactly-once even if the ledger write is lost mid-crash.

## Mock QBO (`src/usali/qbo_mock.py`)

FastAPI app, in-memory state, runnable standalone (`usali qbo-mock --port 9200`) and
in-process for tests. Endpoints (matching Intuit's shapes closely enough that swapping
base URLs later is the only change):

| Endpoint | Behavior |
|---|---|
| `POST /oauth2/v1/tokens/bearer` | issues short-lived access token + refresh token; refresh grant supported; expired token → 401 |
| `POST /v3/company/{realm}/journalentry` | validates balanced Line debits/credits and known account refs; honors `requestid` dedup (same id → same response, no double post); assigns JE id |
| `GET /v3/company/{realm}/query?query=select * from Account` | returns the placeholder chart of accounts |
| fault injection | per-request header (e.g. `X-Mock-Fault: throttle|expired-token`) triggers 429 with Retry-After / 401 — exercises backoff and refresh paths |

## GL mapping curation

- Populate `gl_account_code` on every revenue/tax/settlement entry in both
  `mapping/opera.yaml` and `mapping/autoclerk.yaml` against a **placeholder chart of
  accounts** (`mapping/qbo_accounts.yaml`: account code, name, type — documented as
  CPA-replaceable; the mock serves this CoA).
- Same review discipline as trx mappings: entries carry confidence/review_status already;
  the coverage report gains a **GL mapping section** (mapped/unmapped codes per source,
  facts affected).

## A/R capture (Opera ledger section)

- Opera Trial Balance adaptor: parse the ledger block (guest ledger, city ledger,
  deposit/advance ledger balances) it currently skips.
- New tables (alembic): `pms_ledger_balance_stage` (property, source, business_date,
  ledger_code, ledger_name, balance, batch, row_hash) and `usali_ledger_balance_fact`
  (promoted, ledger_code canonicalized via a small `mapping/ledgers.yaml`).
- A/R report = latest balances per ledger for the month (+ movement vs first day of
  month). City ledger balance is the A/R proxy the CPA wants.

## CPA monthly pack

`usali cpa-pack --property P --month 2026-07 [--format text|csv|json] [--out DIR]`
(reporting.py queries + render.py renderers, P6 pattern; `--out DIR` writes one file per
report, else stdout concatenated text):

1. **Sales Report** — monthly rollup by USALI line (facts grouped, MTD totals, per-day
   count), grand total reconciling to Σ daily total_operating_revenue.
2. **Sales Tax / Occupancy Filing** — pass-through tax lines by tax code with taxable
   revenue base (room revenue for occupancy tax), monthly totals.
3. **A/R Balances** — ledger balances as of month end + movement over the month.

## CLI additions

| Command | Purpose |
|---|---|
| `usali qbo-push --property P (--date D \| --month M) [--dry-run]` | build + validate JEs; dry-run prints them; push records ledger rows |
| `usali qbo-status [--property P] [--month M]` | ledger view: pushed/failed/stale per date |
| `usali cpa-pack ...` | as above |
| `usali qbo-mock [--port 9200]` | run the mock server standalone |

Config (`config.py` settings/env): `USALI_QBO_BASE_URL`, `USALI_QBO_CLIENT_ID/SECRET`,
`USALI_QBO_REALM_ID`, `USALI_QBO_REFRESH_TOKEN` — defaults point at the mock.

## Portal additions

- **`/reports` page** — month + property picker; renders the three pack reports (new
  GET `/api/cpa-pack?property=&month=`); a JSON download button ships the API payload; CSV files stay a CLI concern (`cpa-pack --format csv --out DIR`) — amended at implementation.
- **`/qbo` page** — push-ledger status table (new GET `/api/qbo/status`), per-date
  dry-run preview (GET `/api/qbo/preview?property=&date=`), and a **Push button**
  (POST `/api/qbo/push` — the portal's first write action, confirmed by the user) with
  a confirm dialog showing the JE summary before posting. Unmapped-GL refusal renders
  the worklist inline.
- Nav gains Reports and QBO entries. TS types mirror the new Pydantic models.

## Testing

- **pytest:** JE builder unit tests (balanced, grouping, refusal on unmapped GL);
  push against in-process mock (idempotent re-push no-op, request-hash staleness, 429
  backoff, 401 → refresh → retry, RequestId dedup); A/R adaptor golden numbers from the
  real Opera Trial Balance sample; cpa-pack queries (monthly rollup reconciles to Σ
  daily TOR — the P6 invariant extended to month scope); API endpoint tests incl.
  POST /api/qbo/push.
- **vitest:** Reports + QBO pages against fixtures (status table, preview render,
  confirm dialog flow, refusal worklist).
- **Playwright e2e:** extend the existing suite — mock QBO joins the e2e backend
  launcher; flow: reports page shows the pack → QBO page dry-run preview → Push with
  confirm → status shows pushed + JE id → re-push shows no-op.
- **Gates:** the full P7 set (pytest, ruff, strict mypy, vitest, tsc, oxlint, build,
  playwright).

## Error handling

- Push failures (network, 5xx after retries) → ledger row status `failed` with message;
  CLI exit 1 (`FAILED:` convention); portal renders the message.
- Fact changes after push (request_hash mismatch) → `stale` status, visible in CLI +
  portal; resolution is manual (out of scope: void/amend automation).
- Month push is atomic per date, not per month: each date succeeds/fails independently;
  summary reports per-date outcomes.

## Out of scope (explicit)

- A/P, Inventory, Payroll reports (no data source in any current feed).
- Real Intuit sandbox/production validation (config switch exists; validation is a
  follow-up once credentials + real CoA exist).
- JE amendment/void automation; multi-currency; multi-realm.
- Autoclerk ledger parsing (Opera only for A/R this phase).
- Auth on the portal (unchanged: localhost single-user).

## Definition of done

`usali qbo-push --month` posts balanced, idempotent JEs for both properties to the mock
(re-push = no-op; altered facts = stale flag); coverage shows zero unmapped GL codes for
seeded data; `usali cpa-pack` emits all three reports in all three formats with the
monthly-reconciliation invariant tested; portal Reports + QBO pages work end-to-end in
Playwright including a real push against the mock; all P7 gates green; README + design
doc §9 updated (P0–P8).
