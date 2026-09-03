# Open Hospitality

**The open financial and labor engine for hotels.** Open Hospitality turns raw
PMS reports into a USALI-compliant picture of a property's money and labor — a
Summary Operating Statement, Schedule 14/15 labor cost, per-department analytics,
scheduling, and a time clock — with tenant isolation enforced at the database.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![CI](https://github.com/csharp36/open-hospitality/actions/workflows/ci.yml/badge.svg)](https://github.com/csharp36/open-hospitality/actions/workflows/ci.yml)

**[oh.mandati.ai](https://oh.mandati.ai)** — what the product does and who it
is for.

---

## What it does

- **Ingests PMS reports** (Oracle Opera, AutoClerk, SkyTouch, and an extensible source
  registry) and maps every transaction to the correct **USALI** schedule.
- **Produces the financials** — a Summary Operating Statement with drill-through to
  the staged transactions behind each line.
- **Measures labor** — Schedule 14/15 cost and hours, per-day and per-department
  analytics, target hours against rooms actually sold, overtime and productivity.
- **Runs the workforce** — employee lifecycle, scheduling, and an iPad time-clock
  kiosk with server-enforced punch order.
- **Isolates tenants at the database** — row-level security is the wall, not a code
  convention, so one property can never read another's rows.
- **Seals sensitive PII client-side** — SSN, bank, and tax elections are encrypted
  before they reach the server, which never holds them in plaintext at rest.

## Supported PMS sources

Open Hospitality ingests from **Oracle Opera**, **AutoClerk**, and **SkyTouch**
(Choice / choiceADVANTAGE). All three run through one detection registry — a report's
own header maps to `(pms_source, report_type)` — so every source shares the same
detect → parse → stage → promote path and a single source of truth. Any onboarding
PMS picker should be generated from that same registry rather than a parallel list.

**SkyTouch** delivers a bundled **"Standard Audit Pack"** — one PDF auto-emailed after
the nightly audit, containing many report sections. It is ingested via `process_pack`,
which splits the pack into per-report sections and runs each through the same pipeline.
The wired sections are:

- **Hotel Journal Summary** — the transaction-code financial feed → USALI financial facts.
- **Hotel Statistics** — occupancy / ADR / RevPAR. Its column anchors are located by
  header *shape* rather than a fixed phrase, because a real export repeats the header
  once per section in two variants that differ by a `Current` prefix.

Other sections (housekeeping, in-house, vacant lists, and the like) are skipped. The
SkyTouch transaction-code dictionary (`mapping/skytouch.yaml`) ships seeded
`needs-review`: SkyTouch codes are franchise-configurable, so classifications await
per-property curation.

*Known limitations (SkyTouch):*

- **Segmentation** (Revenue by Market / Revenue by Rate Code) exists in choiceADVANTAGE
  but is not built yet — it must first be enabled in a property's audit pack (an
  onboarding step).
- **CLI / watcher auto-routing** of a dropped file to pack-vs-single is deferred;
  `process_pack` is a standalone entry point for now. (The property registry already
  records `pms_source`, the intended routing key.)
- Section identity comes from a report's **title row**, so a section whose top row is
  not its title would go unrecognised (it is skipped, never mis-parsed). No such section
  exists in the packs seen so far.

**HotelKey** is planned but intentionally **on hold**, pending API access and a real
"Final Audit Report" sample — we don't want to build against a single report shape that
may vary across M3 / Inn-Flow integrations in the wild.

## What we do vs. what we delegate

Open Hospitality is the **system of record and the accounting brain**. It is **not a
payroll processor.**

| We own | We delegate |
|---|---|
| USALI mapping, the operating statement, labor analytics | **Actual payroll disbursement** — computed and paid by your provider |
| Employee data, hours, approvals, the pay period | **Tax calculation and filing** — the provider's gross-to-net |
| Scheduling, time & attendance, the kiosk | Money movement and banking rails |
| Multi-tenant isolation, the PII vault | |

We push an approved pay period to a swappable payroll provider (e.g. ADP or Gusto),
pull back the actual gross-to-net, and show **estimate vs. actual vs. variance** in the
P&L. Payroll is *bought, not built* — we own the data and orchestrate the run; the
provider computes and disburses.

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
