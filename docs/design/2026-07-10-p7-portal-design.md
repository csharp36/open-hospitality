# USALI Engine — P7 Reporting Portal Design

**Date:** 2026-07-10
**Status:** Approved for planning
**Depends on:** P0–P6 (merged; fact tables + `reporting.py` query layer + FastAPI ingest endpoint)

## Goal

A local, read-only web UI over the existing PostgreSQL data so a human can *see* what the
engine knows without the CLI: the Summary Operating Statement, drill-through from any USALI
line to the underlying PMS transactions, the mapping coverage worklist, and drag-and-drop
PDF ingestion. Single user, localhost, no auth.

## Decisions locked with the user

1. **Purpose-built portal** (not Metabase, not hybrid) — extends the existing FastAPI app.
2. **React 19 + TypeScript SPA** built with Vite (not server-rendered templates).
3. **Scope: all four features** — SOS view, drill-through, coverage & exceptions, upload UI.
4. **Testing bar: full E2E** — Playwright driving the real portal against seeded data, plus
   Vitest component tests and backend pytest coverage of the new endpoints.
5. **API approach A:** typed JSON endpoints over the P6 reporting functions with Pydantic
   response models. The CLI's JSON renderers and the portal's API evolve independently.

## Architecture

```
frontend/  (Vite + React 19 + TS + TanStack Query/Router + Tailwind)
    │  dev: Vite proxy → :8100        prod: built assets served by FastAPI StaticFiles
    ▼
src/usali/server.py   create_app() gains a read-only /api router (+ existing POST /ingest)
    ▼
src/usali/portal_api.py   NEW: APIRouter + Pydantic response models (Decimals as strings)
    ▼
src/usali/reporting.py    existing queries + ONE new query: line_transactions(...)
    ▼
PostgreSQL (no schema changes)
```

- **`portal_api.py`** owns the HTTP contract: request validation (property/date/range/edition
  params mirror the CLI's validation rules), response models mirroring the P6 dataclasses
  (`SosReport`, `CoverageReport`) field-for-field, and error mapping (reporting `ValueError`
  → HTTP 422 with the message; unknown property/no facts → 404).
- **`reporting.py`** stays the single source of truth for queries. Portal adds:
  - `line_transactions(session, *, property_id, major, sub_category, line_item, date_from,
    date_to) -> list[StagedTxn]` — joins `usali_financial_fact.stage_id →
    pms_daily_financial_stage` filtered to the facts behind one SOS line; returns staged
    code, description, amount, business_date, batch metadata. Frozen dataclass, pure read.
  - `list_properties(session) -> list[PropertyInfo]` — distinct property/source pairs with
    available business-date range (min/max) so the UI can populate pickers without guessing.

## Endpoints (all GET unless noted; all under /api)

| Endpoint | Backing query | Notes |
|---|---|---|
| `/api/properties` | `list_properties` | picker bootstrap: property_id, pms_source, date range |
| `/api/sos?property=&date=` or `?from=&to=` | `summary_operating_statement` | exactly-one-of validation as in CLI |
| `/api/sos/line/transactions?property=&major=&sub=&line_item=&from=&to=` | `line_transactions` (new) | drill-through |
| `/api/coverage?edition=12` | `coverage_report` | |
| `POST /ingest` | existing | unchanged; upload page consumes it |

Decimals serialize as strings everywhere (P6 convention: exact, never floats).

## Frontend

`frontend/` at repo root — Vite + React 19 + TypeScript, TanStack Query (server state),
TanStack Router (3 routes), Tailwind v4. No component library initially; the statement
table and worklist are bespoke, small, and styled directly.

- **`/` SOS page** — property picker + single-date/range toggle (populated from
  `/api/properties`); renders the statement: operated departments with subtotals, misc
  income, TOTAL OPERATING REVENUE, rooms segment split with %, taxes, settlements, other,
  statistics table (prior-year columns only when present). Every financial line is
  clickable → drill-through panel (slide-over) listing the staged PMS transactions with
  code/description/amount and a sum that reconciles to the line total.
- **`/coverage` page** — per-source cards: confidence + review-status breakdowns,
  staged-vs-mapped with missing list, exception count, needs-review worklist table,
  segment/statistics coverage. Edition selector (default 12).
- **`/upload` page** — drag-and-drop (or file picker) → `POST /ingest`; shows the returned
  result card (source, report type, property, date, staged/mapped/unmapped/skipped) per
  file; failures render the 422 detail inline.
- API client: hand-written typed fetch wrappers matching the Pydantic models (no codegen —
  five endpoints does not justify an OpenAPI toolchain).

## Dev & serve wiring

- `npm run dev` in `frontend/` proxies `/api` + `/ingest` to `uvicorn` on :8100.
- `npm run build` emits `frontend/dist/`; `create_app` mounts it via `StaticFiles(html=True)`
  when the directory exists (falls back to API-only when absent, so backend tests and
  CLI-only users never need node).
- `usali serve` (existing command) becomes the single way to run the portal.

## Testing

- **Backend (pytest, existing stack):** endpoint tests via FastAPI TestClient over the
  seeded six-PDF fixture — SOS JSON matches the known totals (10866.37 etc.), drill-through
  rows sum to their line, coverage shape, validation errors (422/404), properties list.
  `line_transactions` unit-tested in `test_reporting_*` style.
- **Frontend (Vitest + Testing Library):** statement layout renders sections/totals from a
  fixture SosReport; drill-through panel reconciliation; upload result/error cards.
- **E2E (Playwright):** against `usali serve` + Testcontainers Postgres: upload a real
  sample PDF → navigate SOS → assert TOTAL OPERATING REVENUE visible → click the Parking
  line → staged transaction appears → coverage page shows the needs-review worklist.
  Runs headless as its own gate (`npm run e2e` from `frontend/`, which boots the backend
  against a dedicated Testcontainers Postgres via a small launcher script).
- **Gates:** existing (pytest, ruff, strict mypy) plus `tsc --noEmit`, oxlint (Vite scaffold default; plan said eslint), vitest,
  playwright.

## Error handling

- Reporting `ValueError`s (bad range, no facts, multi-source) → 422 with the message text.
- Unknown property → 404. Malformed dates → 422 from FastAPI/Pydantic validation.
- SPA shows inline error states (TanStack Query `error`), never blank screens.
- `POST /ingest` failures surface the existing 422 detail per file.

## Out of scope (explicit)

- Auth / multi-user / HTTPS — localhost single-user tool.
- Metabase or any BI container.
- Exports UI — the CLI (`usali export`) covers flat exports.
- Any write path other than PDF upload; the portal never edits mappings or facts
  (worklist curation stays in YAML + `seed-mappings`).
- Statistics/segments drill-through — financial lines only this phase (statistics and
  segment facts already show their values directly; revisit if needed).

## Definition of done

All four pages functional against the six seeded sample PDFs; drill-through sums reconcile
to SOS lines; full backend suite + FE unit + Playwright E2E green; ruff/mypy/tsc/oxlint
clean; README gains a "Portal" section; design doc §9 roadmap updated to P0–P7.
