# Open Hospitality

**The open-source accounting and labor platform for hotels.** Open Hospitality
turns raw PMS reports into a USALI-compliant picture of a property's money and
labor — a Summary Operating Statement, Schedule 14/15 labor cost,
per-department analytics, scheduling, and a time clock — with tenant isolation
enforced at the database. It is the system of record for a hotel's operations,
and it is becoming the system of record for its books: a hotel-native general
ledger with bank feeds and reconciliation is on the
[roadmap](docs/ROADMAP.md).

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/csharp36/open-hospitality/actions/workflows/ci.yml/badge.svg)](https://github.com/csharp36/open-hospitality/actions/workflows/ci.yml)

**[oh.mandati.ai](https://oh.mandati.ai)** — what the product does and who it
is for.

---

## Where it sits

A hotel's software stack has four layers, and knowing which one you are
looking at explains most of what this project does and does not build:

| Layer | What it is | Examples |
|---|---|---|
| **CRS** (central reservation system) | The brand's booking engine and distribution hub — where reservations, rates, and loyalty live before they reach the property. Franchisors own theirs, and only an approved PMS may connect to it. | Brand-operated |
| **PMS** (property management system) | The front desk's system of record: reservations, check-in, folios, room status, and the **night audit** — the end-of-day close that rolls the business date and produces the standard report pack. | Oracle OPERA, HotelKey, choiceADVANTAGE, AutoClerk, Cloudbeds |
| **Accounting & labor** — *this project* | Turns the night-audit pack into USALI facts: the operating statement, labor cost per occupied room, budgets and variance, reconciliation, alerts. | Open Hospitality |
| **GL** (general ledger) | The accounting system of record for money: the journal, the trial balance, the balance sheet, the year-end package. | QuickBooks Online, NetSuite — and, per the roadmap, Open Hospitality itself |

**USALI** is the Uniform System of Accounts for the Lodging Industry (HFTP,
11th edition): the chart-of-accounts and statement structure hotel owners,
lenders, and brands all expect. Every transaction Open Hospitality ingests is
mapped to its USALI schedule and line; Schedules 14 and 15 are the labor
schedules it computes natively.

Open Hospitality is the accounting and labor layer between the PMS and the
ledger — and, as the general-ledger work lands (OH-27 through OH-30 on the
[roadmap](docs/ROADMAP.md)), the ledger too: one system where the operating
data and the books live together, with QuickBooks kept as an optional export
rather than a requirement. It is deliberately **not** a PMS, a booking engine,
or a payroll processor.

## What it does

- **Ingests PMS reports** (Oracle OPERA, AutoClerk, choiceADVANTAGE, and an
  extensible source registry) and maps every transaction to the correct
  **USALI** schedule.
- **Produces the financials** — a Summary Operating Statement with
  drill-through to the staged transactions behind each line, plus occupancy,
  ADR, RevPAR, and TRevPAR with period comparisons.
- **Measures labor** — Schedule 14/15 cost and hours, per-day and
  per-department analytics, target hours against rooms actually sold, overtime
  and productivity.
- **Runs the workforce** — employee lifecycle, scheduling, and an iPad
  time-clock kiosk with server-enforced punch order.
- **Connects the hotel's own accounts** — each tenant authorizes its own
  QuickBooks Online, Gusto, or ADP connection from inside the app, with
  credentials encrypted per organization.
- **Isolates tenants at the database** — row-level security is the wall, not a
  code convention, so one property can never read another's rows.
- **Seals sensitive PII client-side** — SSN, bank, and tax elections are
  encrypted before they reach the server, which never holds them in plaintext
  at rest.

## Supported PMS sources

Open Hospitality ingests from **Oracle OPERA**, **AutoClerk**, and
**choiceADVANTAGE** (Choice Hotels' franchise PMS). All three run through one
detection registry — a report's own header maps to `(pms_source,
report_type)` — so every source shares the same detect → parse → stage →
promote path and a single source of truth. Any onboarding PMS picker should be
generated from that same registry rather than a parallel list.

> **Naming note:** choiceADVANTAGE appears in the code as the source
> identifier `SKYTOUCH` (and `mapping/skytouch.yaml`) — an earlier
> misattribution. The reports it parses are choiceADVANTAGE's audit pack;
> SkyTouch Hotel OS is a separate product, sharing lineage but not report
> shapes, and is not built. The identifier rename is a tracked task on the
> [roadmap](docs/ROADMAP.md).

**choiceADVANTAGE** delivers a bundled **"Standard Audit Pack"** — one PDF
auto-emailed after the nightly audit, containing many report sections. It is
ingested via `process_pack`, which splits the pack into per-report sections
and runs each through the same pipeline. The wired sections are:

- **Hotel Journal Summary** — the transaction-code financial feed → USALI
  financial facts.
- **Hotel Statistics** — occupancy / ADR / RevPAR. Its column anchors are
  located by header *shape* rather than a fixed phrase, because a real export
  repeats the header once per section in two variants that differ by a
  `Current` prefix.

Other sections (housekeeping, in-house, vacant lists, and the like) are
skipped. The transaction-code dictionary (`mapping/skytouch.yaml`) ships
seeded `needs-review`: choiceADVANTAGE codes are franchise-configurable, so
classifications await per-property curation.

**Where connectivity is headed.** The target is seven PMS integrations —
**HotelKey** (including Hilton's PEP and BWH's AutoClerk Atlas),
**choiceADVANTAGE**, **Oracle OPERA** (report files today, the OHIP API next),
**Sabre SynXis Property Hub**, **Marriott FOSSE / Agilysys Stay**, **Visual
Matrix**, and **Cloudbeds** — reaching most branded US properties, with the
independent tail served through integration aggregators rather than
ever-more parsers. Most of these systems deliver the same few night-audit
report shapes by scheduled email, so each new source is a parser and a mapping
dictionary, not a new architecture. HotelKey is first: it is the one brand
platform that is API-first for accounting data, and credentials are
property-initiated.

## What we own vs. what we delegate

Open Hospitality is the system of record for the hotel's operations and — as
the ledger ships — its books. It is **not a payroll processor** and **never
holds bank credentials**.

| We own | We delegate |
|---|---|
| USALI mapping, the operating statement, labor analytics | **Actual payroll disbursement** — computed and paid by your provider |
| Employee data, hours, approvals, the pay period | **Tax calculation and filing** — the provider's gross-to-net |
| Scheduling, time & attendance, the kiosk | **Bank connections** — read-only, through an aggregator (Plaid-style); the aggregator's consent screen holds the credential, we store only a revocable token |
| The general ledger: journal, chart of accounts, close *(in progress — see roadmap)* | **AP capture and bill payment rails** — OCR and money movement through a connected provider; we own the coding and the ledger effect |
| Multi-tenant isolation, the PII vault | |

We push an approved pay period to a swappable payroll provider (e.g. ADP or
Gusto), pull back the actual gross-to-net, and show **estimate vs. actual vs.
variance** in the P&L. Payroll is *bought, not built* — we own the data and
orchestrate the run; the provider computes and disburses. The same boundary
recurs everywhere money moves: Open Hospitality keeps the books; it does not
touch the money.

## Quickstart

```bash
uv sync                              # Python deps (creates .venv)
cd frontend && npm install && cd ..  # frontend deps
scripts/dev.sh start                 # containers → migration → API → mocks → frontend
```

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), Node 20+. The full setup,
architecture, and the RLS/tenancy model are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Community

- 💡 **Request a feature or share an idea** → [Discussions › Ideas](../../discussions/categories/ideas)
- 🐛 **Report a bug** → [open an issue](../../issues/new/choose)
- 🗺️ **See what's planned** → the [Roadmap](../../discussions/categories/roadmap) — an idea already on the roadmap will be linked and closed as a duplicate, not lost
- 💬 **Ask a question** → [Discussions › Q&A](../../discussions/categories/q-a)
- 🔒 **Report a vulnerability** → **do not** open a public issue; see [SECURITY.md](SECURITY.md)

New contributors: start with **[CONTRIBUTING.md](CONTRIBUTING.md)**. All contributions
require signing our [Contributor License Agreement](CLA.md) — the CLA bot will guide you
on your first pull request.

## License

Apache License 2.0 — see [LICENSE](LICENSE). Premium/hosted modules, where offered, are
licensed separately.
