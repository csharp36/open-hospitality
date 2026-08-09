# open-hospitality — Documentation

Local, no-cloud pipeline that ingests hotel PMS files (Opera 5.6, Autoclerk),
maps each transaction to a USALI schedule/line-item, and loads a unified
financial database.

## Layout

```
docs/
├── reference/
│   ├── usali/            USALI standard: editions, schedules, classification rules
│   │   ├── editions.md
│   │   ├── schedules.md
│   │   ├── rooms-schedule.md
│   │   ├── misc-income.md
│   │   └── templates-and-sources.md
│   ├── sources/          The two PMS source systems we ingest
│   │   ├── ingestion-contract.md   the 6 daily PDF reports & their roles
│   │   ├── opera.md                notes on the Opera catalog
│   │   ├── opera/                  raw Opera catalog exports (XML, PDF) — 478 codes
│   │   ├── autoclerk.md            notes on the Autoclerk extract
│   │   └── autoclerk/              raw Autoclerk extract (XLSX, CSV)
│   └── samples/          the 6 daily PDF report deliverables (also used as test fixtures)
└── design/               Architecture & design docs

```

> Note: `reference/samples/` and the raw `reference/sources/{opera,autoclerk}/` files are
> live inputs the pipeline and tests consume — not just documentation.

## Reading order

1. `reference/sources/*` — what the raw inputs look like and their quirks.
2. `reference/samples/*` — the actual daily PDF deliverables we parse.
3. `reference/usali/*` — the target standard we map into.
4. `design/*` — how the engine bridges the two.

> **Authoritative source of truth:** HFTP's paid USALI 12th Revised Edition.
> Anything marked LOW / unverified in `reference/usali/` must be confirmed
> against the official text before being trusted as compliance-grade.
