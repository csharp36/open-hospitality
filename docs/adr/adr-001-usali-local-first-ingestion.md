# ADR-001: USALI standard + local-first PDF ingestion

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

Hotels receive their operating financials as **emailed PDF reports** from their
property-management system (Opera, AutoClerk) — trial balances, manager flashes,
market statistics. Back-office tooling in this space (e.g. Inn-flow) exists largely
to re-key those PDFs into a unified ledger. Two things had to be true from day one:
the data had to land in a schema hospitality accountants already trust, and the
system had to run without any cloud dependency so a single operator could use it.

The industry's shared chart of accounts is the **Uniform System of Accounts for the
Lodging Industry (USALI)**. Buyers, CPAs, and asset managers expect USALI schedules;
a bespoke chart of accounts would not interoperate with anything.

## Decision

We will build a **local-first ingestion pipeline** keyed on the **USALI** schedule of
accounts:

- **Pipeline:** `detect → parse → stage → transform → file`. A DB-backed detection
  registry identifies `(pms_source, report_type, property)` from the PDF header; a
  per-report adaptor parses the template into canonical staged records; `transform`
  joins staged rows to the mapping dictionary and promotes them to Core facts within
  one transaction under one ingest batch.
- **The load-bearing artifact is the `usali_mapping_dictionary`** — a curated
  PMS-code → USALI-account bridge keyed on `(source, trx_code, edition)`. Mapping
  misses route to a `mapping_exception` worklist rather than being dropped.
- **USALI edition is a first-class dimension** — 12th edition default, 11th supported.
- **No cloud in V1**, but the architecture must not preclude it: the same code later
  grew a GCP deployment (ADR-008) and multi-tenancy (ADR-002) without a rewrite.

## Consequences

- Ingestion is **transactional and fail-loud**: a bad file rolls back, records a
  failed batch, and is quarantined — never a partial load.
- Adding a PMS report is a new adaptor + dictionary rows, **not a schema change**.
- Output is directly consumable by USALI-literate accountants and downstream ERP/BI.
- The mapping dictionary is a maintained asset — coverage gaps surface as a
  needs-review worklist, which is ongoing curation work.
- Local-first means the default install carries one Postgres and no external service,
  which keeps the test suite and onboarding simple.

## Alternatives considered

- **Direct PMS API integration** instead of PDFs — rejected: APIs are not universally
  available across PMS vendors/versions, and the emailed PDF is the one artifact every
  property already produces.
- **A bespoke chart of accounts** — rejected: USALI is the standard buyers and CPAs
  expect; anything else fails to interoperate.
- **Cloud-first / SaaS from day one** — rejected for V1: it would have coupled the core
  engine to a hosting model before the ingestion and accounting logic were proven.
