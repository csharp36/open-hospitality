# Build vs. integrate

**Status:** decision record, 2026-07-19. Revisit when a hotel actually asks for one of these.

## The decision rule

| Verdict | When |
|---|---|
| **OWN** | It touches USALI mapping, hours worked, or the confinement rules. Nobody else's system knows a Sysco line is Schedule 2 COGS, or that Vikram's 11 hours span two properties. |
| **INTEGRATE** | A market leader does it well and exposes a public API. We consume it and own the hotel-specific interpretation. |
| **LITE** | Integration overhead exceeds the value at two properties, and it isn't core. |

Stated plainly: **if a competitor's whole company does it, we don't rebuild it — we plug in and own the interpretation layer.**

### The second test, which decides the hard cases

The rule above is necessary but not sufficient. Applied alone it says INTEGRATE
for PTO — Gusto does PTO, has an API, and is a whole company — which is the
wrong answer. So:

> **Does integrating create a second source of truth for a number we compute?**
> If yes, **OWN**, regardless of how mature the market is.

PTO accrual is driven by hours worked and we are the system of record for hours.
Two systems computing a balance from the same input will diverge, and the
divergence surfaces as an employee being told two different numbers. The same
test is why we own USALI mapping and confinement, and why we emphatically do
**not** own payment rails — nobody else computes those, or wants to.

## Verdicts

### AP / invoice automation — INTEGRATE, own the coding

Bill.com, Ramp, Melio; AvidXchange and Nexus are hospitality-specific. They own
capture, OCR, approval routing, and payment rails. We own **USALI line coding**,
which is the mapping engine we already run for PMS data with a different input.
Rebuilding AP is a company, not a phase.

⚠️ **Verify before committing:** which of these expose **line-level** invoice
data rather than header totals. Header-only is useless for USALI coding. Check
against real API docs; do not assume.

### Bank reconciliation — DON'T BUILD

This is GL work and we are deliberately not the GL. QuickBooks does it and we
already push there. The ceiling is surfacing reconciliation status read-only so
a GM sees "March is closed." If a hotel isn't on QBO, the answer is a second GL
adapter, not a bank-rec module.

**The stronger reason is the threat model, not the scope.** Bank rec requires
bank credentials or Plaid-style aggregation. We hold no banking credentials
today, and **Pillar E5 is specifically designed so we cannot**: deposit accounts
are HPKE-sealed client-side and server-opaque. Building bank rec means building
the exact capability E5 exists to deny us. That is a much harder no than "QBO
does it."

### PTO accrual — OWN (Pillar E4)

The one to resist integrating. See the second test above. The scheduler needs
balances at approval time, and PTO cost is a Schedule 14 benefits line.

**Push balances to the provider; never read them back.** Reading back is what
creates the second source of truth.

🚧 **Blocked** on the California sick-leave statute gate — accrual rate, annual
usage cap, and carryover cap must be confirmed against current statute, not
written from memory. See `docs/design/2026-07-18-pillar-e-employment-model-design.md`.

### Document management — INTEGRATE storage, OWN the rules

Drive/Box/S3 for blobs, DocuSign for signature. We store **type, effective date,
expiry, and the alerting rule**. The value is not the blob — it is
"Guadalupe's food handler card expires in 14 days."

**This is a confirmed market gap.** Field research against the incumbent on
2026-07-18 found its Documents tab **empty** while the Payroll tab records
`I-9 Submitted On 07/07/2025` and `W-4 Submitted On 12/23/2025`. The incumbent
keeps dates, not artifacts. See `docs/reference/overtime-jurisdictions.md` for
the research method and `/Volumes/Employees/inn-flow-schema-inventory-*.json`
(off-repo, encrypted) for the full inventory.

⚠️ **I-9s carry retention and separate-storage obligations** and must not
default into a shared Drive folder. Confirm against USCIS guidance before
designing storage — same standard as the CA sick-leave gate, for the same
reason.

### Mobile — LITE (PWA, not native)

Responsive on the existing React 19 portal. No app store, no second codebase.
The kiosk stays a dedicated iPad with different security properties.

What mobile actually serves is *my schedule / my hours / request time off* and
*approve timecards / labor vs. target*. That is a web page.

### Sales CRM — INTEGRATE (read-only demand feed)

Not Salesforce. Hotel-specific: Amadeus Delphi, Tripleseat. We want exactly one
thing: **forward-looking demand.** Group blocks and booking pace are what make
labor forecasting good — a 200-room block landing Thursday is what tells the GM
to add three housekeepers.

Read-only feed into the scheduler, not a CRM. Cheapest integration on the list
and probably the highest product leverage.

### Slack / monday.com — INTEGRATE (notification port)

Slack for variance-over-threshold, timecard awaiting approval, pay run ready.
monday.com for onboarding checklists and maintenance tickets from our events.

⚠️⚠️ **A notification is a disclosure surface, and this is the item most likely
to undo prior work.**

Outbound payloads must be subject to the **same suppression rules as the API**.
Our suppression is *complementary* — suppressed values are excluded from totals
so subtraction cannot recover them. A webhook saying "Housekeeping labor: $X"
for a department sourcing cost from one priced employee **is** the leak, and
nothing downstream undoes it. That is three Criticals' worth of confinement work
lost to one payload.

It also defeats a property we deliberately built. Every PII read writes an
`AuditEvent`. **We cannot audit who read a Slack message.** So the disclosure is
durable, searchable, exportable, and *outside our audit trail* — three
properties the API surface does not have.

Suppression-aware and idempotent, or it does not ship.

## Architecture

Nothing new required. `payroll_provider.py` established the pattern: a `Protocol`
port, **two deliberately different-shaped adapters** so the abstraction is proven
rather than assumed, a runnable mock, injected at `create_app`. Apply per
category — `ap_provider`, `document_store`, `crm_feed`, `notification_sink`.

## Sequencing

Ordered by **risk-adjusted** value, not by size.

1. **CRM demand feed** — read-only, inbound, *zero disclosure surface*, and it
   makes the scheduler materially smarter. Lowest risk per unit of value.
2. **Slack notifications** — daily value and a small surface, but it is the only
   **outbound** disclosure path here. Doing it first would mean building the
   riskiest item before the notification port has any usage to shape it, in the
   one part of the system that has already produced three Criticals.
3. **Document metadata + expiry** — real compliance value, confirmed market gap.
4. **AP line coding** — biggest lift, biggest competitive gap.
5. **Bank rec status** — read-only, near-zero effort.
6. **PWA polish** — continuous.

## Overlap with Pillar E

Three of these are already partly committed inside E, so they are continuations
rather than a fresh roadmap:

- **PTO** *is* E4
- **Document metadata** overlaps E3's `i9_submitted_on` / `w4_submitted_on`
- **Deposit accounts** in E5 sit adjacent to the AP/payment-rails boundary

## The standing cost nobody estimates

Each integration is a credential to rotate, a DPA or BAA to sign, a rate limit, a
breaking change, and an on-call surface. **Six is a substantial standing
maintenance load for a solo maintainer, and it never appears in the build
estimate.**

Each is also a *compliance* surface: a DPA with Slack means employee data flows
to a subprocessor, and for a two-property operation with 51 employees that is
real paperwork per vendor.

**One at a time, mock first, and ideally only when a real hotel is asking for it.**
