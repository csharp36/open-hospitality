# Data posture & progressive onboarding — decision (D8)

Status: **DECIDED 2026-08-16.** Resolves the "PROD-only vs sandbox" fork
left open in [`2026-08-01-onboarding-flow-design.md`](2026-08-01-onboarding-flow-design.md)
§2 (the lifecycle spine) and §6 (the promotion seam). Sits alongside the
D1 (tenant isolation) and D2 (Keycloak tenancy) decision docs as a
load-bearing constraint on the whole onboarding milestone (OH-1).

## The question

The 2026-08-01 draft assumed a `Visitor → Sandbox tenant → Prod tenant`
spine, where "sandbox" was a distinct, mock-wired, time-boxed
environment that an operator later *promotes* to prod in one flip. The
open fork: is a separate non-prod tier worth its complexity, or should
we treat everything as production from byte one?

## Decision

### D8.1 — One data-security posture: always sensitive

There is **no lower-trust data tier.** Every tenant is secured,
retained, deleted, and residency-bound at full production grade from the
first byte. We retire the parallel "mock data vs real data" *system*.

**Rationale.**
- **Legal obligations attach to the *nature of the data*, not the
  *label of the environment*.** Under GDPR (and CCPA/PCI), a box called
  "sandbox" that *can* receive real personal data carries the full
  weight — lawful basis, security, breach notification, erasure,
  retention limits, residency — and gets none of the safety, because
  test tiers are historically the least secured and sit outside the
  retention/deletion discipline. "It was only our test env" is not a
  defense; knowing real data could land there and securing it less is
  aggravating.
- **We already know real sensitive data shows up.** A raw night-audit
  export carries **guest PII and card PANs** (the real SkyTouch audit
  pack used during PMS work contained real guest names and card
  numbers). Any surface that accepts an upload is therefore potentially
  in **PCI-DSS and GDPR scope**. There is no "safe" low-security upload
  tier to be had.

### D8.2 — No global sandbox→prod flip; per-integration progressive onboarding

The mock→real transition is **not** a single promotion event. Each
integration (payroll, accounting, CRM/demand, billing, and the PMS
connection itself) carries **its own lifecycle state per tenant**, and
onboarding is a **persisted, resumable checklist of open items**.

- The operator reaches a **working portal immediately.** The core —
  upload a PMS export → real USALI revenue/statistics reporting — needs
  **zero integrations configured.**
- Each unconfigured integration is an **open item** the operator closes
  whenever they choose (or never, if they don't use it — e.g. payroll is
  optional forever; billing is an open item that becomes required at
  trial end).
- **"Fully prod" is not a state — it is zero open items.** Celebration
  ("confetti") fires on real milestones (first report ingested; zero
  open items), not on a big-bang promotion.
- This extends the existing `onboarding.py` and the config-selected
  provider seam (`USALI_CRM_PROVIDER=delphi|""`, mock gusto/adp/qbo)
  the 2026-08-01 doc already relies on — a state model + checklist UI
  over machinery that exists, not a new subsystem.

### D8.3 — Unconfigured integration on a real tenant is *off*, not mock

For a **real tenant**, an integration that isn't yet configured is
**honestly absent**, never faked. A labor-cost line with no payroll
source reads "Connect payroll to see this" — it never shows a synthetic
figure.

**Rationale.** Injecting mock numbers (e.g. sample ADP payroll) into a
real operator's analytics next to their real revenue violates the
standing **reconciliation principle** — operators compare our output to
their own Excel or their M3/Inn-Flow integration, so we must never
surface a figure that isn't theirs. A fake number is worse than an
honest blank.

- **Mock/synthetic data is confined to the canned demo** (the
  `synthetic_year` seed), which **never accepts uploads** and is the
  only genuinely-mock surface.
- Sample UI *shape* previews ("here's what the payroll tab will look
  like") are permissible only when clearly labeled and **never mixed
  into real analytics**.

### D8.4 — Consequence: minimize sensitive data at the boundary

Because we assume sensitive data always, we minimize it at ingestion:

- **Detect-and-redact guest identity + card PANs at the ingestion
  boundary.** The product needs the *financials* (transaction codes,
  amounts, occupancy) — not guest names or card numbers — so identity is
  stripped/tokenized before storage, shrinking the PCI/GDPR footprint.
  (Employee identity, which labor analytics genuinely need, is handled
  under the roster with its own protection — not stripped.)
- **The anonymous parse-preview persists nothing.** The pre-signup
  "drop a file → see it mapped" moment runs **in-memory only** and
  discards everything, so "try before you sign up" carries no storage
  liability.

## Effect on the 2026-08-01 onboarding-flow doc

- **§2 spine:** "Sandbox" is no longer a separate environment. It is a
  tenant lifecycle *state* (open items outstanding) inside the single
  always-sensitive system.
- **§6 promotion seam:** replaced by the per-integration open-items
  checklist; "promotion" dissolves into closing items.
- **Resolves/absorbs:** D3 (sandbox data — no separate sandbox data
  regime; real uploads are secured, not sandboxed), and reshapes D6/D7
  (API scope, time-box) around the open-items model. D1/D2 stand
  unchanged and now apply uniformly (there was never two regimes to
  reconcile).

## What survives from the old model

- The **mock providers** (mock gusto/adp/qbo/delphi/tripleseat) remain —
  but only as the backing for the **canned demo**, not as a per-tenant
  data mode.
- The **config-selected-provider seam** is reused as the per-integration
  *off → real* switch that the open-items checklist drives.
