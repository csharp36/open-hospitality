# open-hospitality — Design

*Date: 2026-07-08 · Status: approved (pre-plan)*

## 1. Purpose & scope

A **local, no-cloud Python pipeline** that replicates the ingestion Inn-flow performs:
hotels email daily PMS reports (as **PDF**), and the system maps each financial transaction
to a **USALI** schedule/line-item and loads a **unified financial database** (Postgres) that
ERP/BI can read.

The load-bearing artifact is the **`usali_mapping_dictionary`** — the curated bridge from
per-PMS transaction codes to USALI classifications.

**In scope (V1):** file-based PDF ingestion for two PMSs (Opera 5.6, Autoclerk), the
Financial report end-to-end, then Statistics and Segmentation reports.
**Out of scope (deferred):** API-based adaptors, cloud deployment, multi-tenant concerns
beyond multi-property data. Architecture must not preclude these.

## 2. Ingestion contract

Input is **PDF** (the exact files emailed to Inn-flow; see
`../reference/sources/ingestion-contract.md`). Three report families per PMS:

| Role | Opera | Autoclerk | Feeds |
|------|-------|-----------|-------|
| **Financial** | Trial Balance | Transaction Summary | codes/names + daily $ → stage → USALI fact |
| **Statistics** | Manager Flash | Manager Report | ADR / RevPAR / occupancy / room counts |
| **Segmentation** | Market Code Statistics | Revenue by Rate Plan | Rooms Transient/Group/Contract split |

Only the **Financial** report carries transaction codes, so it alone drives the mapping →
fact pipeline. The Opera XML catalog (478 codes) seeds the *full* dictionary; the Trial
Balance exercises the codes with daily activity. Autoclerk exposes no codes → synthetic
`CATEGORY|NAME` keys.

USALI target: **12th edition default, 11th supported** — `usali_edition` is a first-class
dimension (see `../reference/usali/editions.md`).

## 3. Architecture

```
        ┌─ email/drop ─┐
 PDFs ─▶│ curl POST    │─▶ inbox/ ─▶ [detect (source, report-type)]
        │ sentinel dir │                     │
        └──────────────┘                     ▼
                              [PDF adaptor: template parser]  ── malformed ─▶ failed/
                                             │
                                             ▼
                              [validate & normalize (Pydantic)]
                                             │  → pms_daily_financial_stage
                                             ▼
                              [USALI transform: join dictionary]
                                    │                    │
                          mapped ───┘                    └── unmapped ─▶ mapping_exception
                                    ▼
                          usali_financial_fact ──▶ Summary Operating Statement / ERP / BI
```

## 4. Components (each independently testable)

| Unit | Responsibility | Depends on |
|------|----------------|-----------|
| **adaptors/** | One PDF template parser per `(source, report_type)`. `opera_trial_balance.py`, `autoclerk_transaction_summary.py` first. Emit canonical `StagedRecord`s. No DB. | pdfplumber, pydantic |
| **normalize** | Sign conventions (`$ -1,234` vs `- 1,234`), date parsing, property_id, source/report tags, Autoclerk synthetic keys. | adaptors |
| **stage repo** | Persist `StagedRecord`s → `pms_daily_financial_stage`; idempotent by `(source, business_date, row_hash)`. | SQLAlchemy |
| **mapping/** | Curated YAML seeds (`opera.yaml`, `autoclerk.yaml`, `usali_schedules.yaml`) + loader (validate → upsert). Draft generator turns the Opera XML catalog into pre-classified YAML (`confidence: LOW`) for human curation. | pydantic, PyYAML |
| **transform** | Join stage → dictionary on `(source, trx_code, edition)`; write `usali_financial_fact`; route misses → `mapping_exception`; reconciliation check (Σ fact == Σ stage per source/date). | stage + mapping |
| **ingestion** | `watch` (sentinel dir, watchdog) and `serve` (FastAPI `/ingest`) intake; batch tracking; failed-file quarantine. `serve` also hosts the read-only portal API (`/api/*`) and, when `frontend/dist/` exists, the portal SPA (P7). | adaptors, transform |
| **cli** | Typer app: `ingest`, `seed-schedules`, `seed-mappings`, `gen-opera-draft`, `transform`, `process`, `watch`, `serve`, `report`, `coverage`, `export`, `cpa-pack`, `qbo-push`, `qbo-status`, `qbo-mock`. | all |

**Design boundary rule:** adaptors know PDFs but not the DB; transform knows the DB but not
PDFs; the mapping dictionary is data, not code.

## 5. Data model

Refines the starting DDL; adds edition/confidence, an output fact table, a schedule
reference table, and audit/exception tables.

- **`pms_daily_financial_stage`** — starting columns + `report_type`, `source_file`, `ingest_batch_id`, `row_hash`.
- **`usali_mapping_dictionary`** — starting columns **+ `usali_edition`, `confidence` (HIGH/MEDIUM/LOW), `review_status`, `notes`**. `UNIQUE(pms_source, pms_trx_code, usali_edition)`.
- **`usali_schedule`** *(new)* — `schedule_id`, `usali_edition`, `name`. Seeded with the 16 schedules per `../reference/usali/schedules.md`.
- **`usali_financial_fact`** *(new, output)* — property, business_date, source, edition, schedule_id, major/sub/line_item, amount, room_count, gl_account_code, ingest_batch_id, stage_id.
- **`mapping_exception`** *(new)* — unmapped `(source, trx_code, description, amount, business_date, batch_id)`. Nothing is ever silently dropped.
- **`ingest_batch`** *(new)* — one row per processed file: source, report_type, file, hash, status, row counts, timestamps.

Autoclerk `pms_trx_code` = synthetic `CATEGORY|NAME` (e.g. `ROOM|ROOM_RENT`).

## 6. Mapping dictionary authoring (curated YAML)

- Human-authored, git-reviewed `mapping/opera.yaml` & `mapping/autoclerk.yaml`; each entry
  carries `usali_edition`, `confidence`, `notes`.
- `usali gen-opera-draft` parses the 478-code XML catalog → emits YAML pre-classified by
  Opera group (`ROOM`→Sch 1, `TAX`→Sch 4, `TELEPHONE`→Sch 6, …) at `confidence: LOW` for a
  human to curate — avoids hand-typing 478 rows.
- Encoded ground-truth rules (from research): resort/facility fees → Sch 4 (excluded from
  ADR); individual no-show/cancellation → Sch 1 Other Rooms Revenue; group cancellation →
  Sch 4. Parking/pet fees seeded **LOW / needs-review** pending official-text confirmation.
- Opera Trial Balance's own Revenue/Non-Revenue/Payment buckets are used as a cross-check:
  revenue codes must land on revenue schedules; payment codes must not map to revenue.

## 7. Integrity & error handling (financial data — non-negotiable)

- **Idempotency:** re-ingesting the same file (hash + source + date) is detected; no double-count.
- **No silent drops:** every unmapped code → `mapping_exception`; transform exits non-zero if exceptions exist (configurable gate).
- **Reconciliation:** post-transform assertion that mapped totals equal staged totals per source/date; discrepancies surfaced.
- **Bad files:** quarantined to `failed/` with an error record on `ingest_batch`.

## 8. Tech stack (local, "build it right")

Python 3.12 · SQLAlchemy 2.0 + Alembic (migrations) · Pydantic v2 · Typer (CLI) ·
FastAPI + uvicorn (curl intake) · watchdog (sentinel) · **pdfplumber** (PDF parsing) ·
PyYAML · openpyxl (catalog xlsx, optional) · pytest + Testcontainers-Postgres · ruff + mypy ·
**Docker Compose** for local Postgres. TDD throughout; the six PDFs in `docs/reference/samples/` are the test fixtures.

## 9. Phasing

| Phase | Deliverable |
|------:|-------------|
| **P0** | Repo scaffold, docker-compose Postgres, Alembic schema (all §5 tables), CLI skeleton, ruff/mypy/pytest harness, seed `usali_schedule`. |
| **P1** | Opera **Trial Balance** PDF → stage; seed dictionary from XML catalog (`gen-opera-draft` + curated `opera.yaml`); transform → `usali_financial_fact`; exceptions + reconciliation working end-to-end. |
| **P2** | Autoclerk **Transaction Summary** PDF → same pipeline with synthetic keys + curated `autoclerk.yaml`; validated against real daily totals. |
| **P3** | Ingestion ergonomics: sentinel `watch` + `serve` (curl/email-drop), `ingest_batch` tracking, failed-file quarantine. |
| **P4** | **Statistics** reports (Manager Flash / Manager Report) → ADR/RevPAR/occupancy tables. |
| **P5** | **Segmentation** reports (Market Code Statistics / Revenue by Rate Plan) → Rooms Transient/Group/Contract split; completes a faithful USALI Rooms schedule. |
| **P6** | Reporting/export: Summary Operating Statement rollup, ERP/BI CSV/JSON, mapping-coverage & confidence report. |
| **P7** | **Reporting Portal** — a local web UI to *see* reports against the DB. Recommended approach: a purpose-built read-only portal (extend the P3 FastAPI app with read endpoints + a lightweight web UI) rendering the **Summary Operating Statement** (by property / date / edition), per-schedule drill-downs, **mapping coverage + exceptions**, and drill-through from a USALI line back to the underlying PMS transactions. Optionally pair with **Metabase** (docker-compose) for ad-hoc exploration. Portal-vs-Metabase-vs-hybrid to be finalized when this phase is planned. |
| **P8** | **QBO push + CPA monthly report pack** — post normalized USALI data into QuickBooks Online (Journal Entries via the QBO API, OAuth2, idempotency keys against double-booking). The dictionary's `gl_account_code` column becomes load-bearing: curated USALI-line → QBO chart-of-accounts mapping, same review discipline as the trx dictionary. CPA monthly pack coverage from current sources: **Sales Report** ✅ (financial facts rollup); **Sales Tax / Occupancy Filing** ✅ (pass-through tax lines + taxable revenue per jurisdiction); **A/R** ⚠ partial (city-ledger balances/aging exist in the PDFs — Opera Trial Balance ledger section currently skipped, would start capturing); **A/P** ❌ (not in any PMS feed — comes from QBO/vendor bills); **Inventory** ❌ and **Payroll MTD/YTD** ❌ (new source-adaptor families, e.g. payroll-provider exports → USALI Sch 14). |

Each phase gets its own spec → plan → implement cycle. This document is the umbrella +
Phase 1 detail.

**Status: P0–P8 delivered.**

> **Optional early quick-look:** because Metabase needs no app code, a dockerized Metabase
> pointed at the Postgres can be slotted in as soon as P1 data exists — well before P7 — to
> eyeball `usali_financial_fact` sooner. Not yet scheduled; enable on request.

## 10. Open items (non-blocking)

- Confirm Schedules 12–14 naming, and parking/pet-fee/early-check-in classification against
  the paid HFTP USALI 12th edition text (see `../reference/usali/templates-and-sources.md`).
- Optional Autoclerk supervisor/config export with real code IDs (would replace synthetic keys).
- ~~Transform idempotency (from P1 review)~~ **Done in P3:** outputs track `stage_id`
  uniquely and `transform` skips already-processed rows (`TransformResult.skipped`), so
  automated re-processing is a safe no-op; reconciliation still verifies persisted totals.
- ~~Business-date extraction (from P1)~~ **Done in P2:** `ingest` derives the business date
  from the report header via per-adaptor `extract_business_date` (Opera MM-DD-YY, Autoclerk
  MM/DD/YYYY). Future report types add their own extractor as they land.
