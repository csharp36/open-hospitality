# Open Hospitality — roadmap and sequencing

Status: **REVISED 2026-09-06.** This revision supersedes the 2026-08-30 gap
analysis (preserved in git history at `5b01df5`). That document asked what
stood between the demo and a hotel that discovers the product, tries it, signs
up, connects its real systems, and pays; everything it verified about the
code's state is carried forward here unchanged. What changes is the sequencing
above that state, driven by the September positioning analysis at
[`reference/competitive-positioning-2026-09.md`](reference/competitive-positioning-2026-09.md).

## Relationship to `.github/roadmap.yml`

[`.github/roadmap.yml`](../.github/roadmap.yml) stays the **single source of
truth** the triage bot dedups feature requests against. It is a flat catalogue
of capabilities with stable `OH-<n>` ids. This document is the **ordering over
that catalogue**: what is genuinely missing, which items silently block
others, and what should be built first. Where the two disagree, the yml wins
on *what a capability is*; this doc wins on *what state it is in and when it
should land*. §7 records the deltas applied back to the yml in this revision.

---

## 1. What changed on 2026-09-06

Four decisions, each argued in full in the positioning analysis. Recorded here
because they re-order everything below.

### 1.1 The layer, named — and extended downward

Open Hospitality sits in the layer the industry calls hotel accounting or
back-office software: **the accounting and labor layer between the PMS and the
general ledger**. That has been true since the first USALI mapping shipped.
What is new is the decision to extend downward: OH will carry a
**full-featured general ledger** (OH-27 through OH-30), built in stack-ranked
slices, so an owner with one to ten properties can keep the books in OH,
reconcile them to the bank, and hand their CPA a year-end package — without a
separate ledger product. "Full-featured" is deliberately narrowed to one
industry, one chart of accounts (USALI), and a known set of transaction
sources; the out-of-scope list in §8 is what keeps it from growing by
accretion into a general-purpose ledger.

The reasoning, in short: OH already computes and emits a journal every day
(the QuickBooks push), so the posting model exists and merely lacks a home;
and a multi-property owner's real pain is the books and the operating data
living in different systems. The QuickBooks push stays, as an export *from*
the journal, for owners whose CPA insists.

### 1.2 Two standing decisions are superseded or amended

- **[`reference/build-vs-integrate.md`](reference/build-vs-integrate.md),
  "Bank reconciliation: DON'T BUILD" — superseded.** The verdict rested on
  QuickBooks doing it and on Pillar E5's credential posture. The first is now
  a feature OH provides natively; the second is addressed by the amendment
  below. The rest of that document's verdicts stand (see §8).
- **Pillar E5's bank posture — amended, not reversed.** The property that
  matters is kept: OH never stores an account number, routing number, or
  online-banking credential in a form the server can read, and employee
  direct-deposit details stay sealed client-side per ADR-004. What changes is
  that OH will hold **aggregator access tokens** (Plaid first, Yodlee as a
  second adapter) granting read-only transaction access to *business*
  operating accounts — stored with ADR-005 per-org field encryption like the
  QBO refresh token, revocable from the integrations page, every read written
  to the audit log. A token cannot initiate payment, and the aggregator's own
  consent screen is the credential holder. This needs an ADR before OH-28
  code is written (§6).

### 1.3 Seven PMS integrations, then aggregators

The connectivity target is fixed at **seven integrations**: HotelKey
(including Hilton PEP's emailed pack and BWH's AutoClerk Atlas),
choiceADVANTAGE (built), Oracle OPERA (files built, OHIP next), Sabre SynXis
Property Hub, Marriott FOSSE and its Agilysys Stay successor, Visual Matrix,
and Cloudbeds. Together they reach roughly 60% of US properties and about 80%
of branded ones. Past that, every point of coverage costs a new parser for a
PMS with a few hundred properties — so the independent tail is served through
integration aggregators, not an eighth adapter. Most of the seven deliver by
scheduled email in the same few report shapes already parsed, which is why
the emailed-report intake (OH-23) is on the critical path and why each new
source is a parser plus a mapping dictionary, not a new architecture.

### 1.4 choiceADVANTAGE, not SkyTouch

The source built as "SkyTouch" parses the audit pack that **choiceADVANTAGE**
— Choice's franchise PMS — emails nightly. SkyTouch Hotel OS is a separate
product sold to non-Choice hotels, sharing lineage but not report shapes, and
is *not* built. Docs (this file, the README, `roadmap.yml`) now say
choiceADVANTAGE. The in-code identifier (`pms_source`, `mapping/skytouch.yaml`,
the adapter module names) still says `SKYTOUCH`; renaming it is a tracked
task, sequenced with the mapping-editor schema work (§4) since both touch the
dictionary keying.

### 1.5 Billing stays early — a deliberate departure

The positioning analysis ranks subscription billing (OH-19) late, in Tier 2.
This revision **rejects that ordering**: OH-19 leads Tier 1. The premise of
this roadmap is still a paying tenant, and billing is what turns a tenant into
revenue; it also forces the open-core boundary decision (§6), which must be
settled *before* the hosted bank connection ships, because the aggregator
contract and its compliance envelope are the natural paid line.

---

## 2. Where we are

Verified against the code in the 2026-08-30 analysis; the only code change
since is the `/integrations` page (PR #113), reflected below.

**Shipped:**

- **The engine** — detect → parse → stage → promote, USALI mapping, the
  Summary Operating Statement with drill-through, labor Schedule 14/15,
  per-department analytics, scheduling, the kiosk with enforced punch order,
  payroll orchestration to swappable providers with estimate vs. actual.
- **Three PMS sources** — Opera, AutoClerk, and choiceADVANTAGE (the bundled
  Standard Audit Pack via `process_pack`), through one detection registry.
- **Multi-tenancy** — RLS fail-closed as the tenant wall, Keycloak
  Organizations identity, client-side sealed PII (ADR-004), per-org field
  encryption (ADR-005), pinned by a real two-org isolation suite.
- **Property config and core statistics** — room inventory, fiscal calendar,
  occupancy / ADR / RevPAR / TRevPAR with comparisons (OH-6, most of OH-7).
- **Productization** — the marketing site (OH-16), the anonymous `/try`
  preview, invite-gated signup, the onboarding open-items checklist (OH-18),
  and per-tenant integration config with the `/integrations` page (OH-17).

**Open, carried forward from the previous analysis:**

- **Ingestion-boundary redaction** is preview-only: the authenticated
  `/ingest` path stores the raw uploaded PDF unredacted. A compliance gate on
  the first real tenant's first real upload — now Tier 0 (§3).
- **The invite gate** is a flag lifted at GA by design, but invite creation is
  a CLI command, so approving a signup means an operator shelling into a
  container. The admin surface around the gate is still missing.
- **The notification seam** is transactional only (invites, OTP): no SMS
  vendor, no per-tenant recipients, no digests. Now OH-26, Tier 2.
- **Billing** (OH-19) is greenfield: no plan model, no entitlement check, no
  trial clock, no payment rail. D8 places it as an open item that becomes
  required at trial end — a consumer of the checklist, not a parallel
  subsystem.
- **The mapping dictionary** is a single global table in direct conflict with
  franchise-configurable transaction codes; the org-override schema decision
  (§6) blocks the mapping editor (OH-20).

---

## 3. The spine — sequencing

Ranked by expected contribution to the first cohort of paying tenants, with
dependencies explicit. The general-ledger slices are named G1–G8 in the
positioning analysis; the mapping to roadmap ids is G1 → OH-27, G2 → OH-28,
G3+G4 → OH-29, G5+G6+G7 → OH-30, G8 → the forecasting item in Tier 2.

### Tier 0 — the first feed, the compliance gate, the ledger core

| # | Work | Id | Why here |
|---|---|---|---|
| 1 | **HotelKey integration** — API + event stream where the property grants credentials; a parser for Hilton PEP's emailed audit pack where the API is franchisor-gated. Include settlement-by-payment-type from day one; bank matching (#6) needs it. | OH-22 | Integration #1 of seven: the largest and fastest-growing brand platform, and the only API-first accounting feed among the brand systems. Credentials are property-initiated and already requested. |
| 2 | **Ingestion-boundary redaction on the authenticated path** | — | The gate that lets a stranger upload a real audit pack; the first line of every security review. More important, not less, once OH holds bank tokens. Smallest item on the list. |
| 3 | **General ledger posting core** — USALI chart of accounts with per-org extensions, immutable double-entry journal with source links to staged PMS rows and labor facts, fiscal periods with an audited close, trial balance and balance sheet; the operating statement re-pointed at the journal; the QBO push becomes an export from it. | OH-27 | Everything later posts into it, and it is smaller than it sounds: the fiscal calendar, the USALI dictionary, the staged facts, and the journal generator already exist. Needs the GL posting-model ADR first (§6). **Backend, the /gl page, and the SOS cutover shipped**: the operating statement's totals now render from the journal (shape C of docs/design/2026-09-07-oh27-sos-cutover-decision.md), with tests/test_gl_parity.py staying in CI as the tripwire. |
| 4 | **Emailed-report intake** — an inbound address per property, detection-registry routed. | OH-23 | Four of the seven target PMSs deliver by scheduled email; until this exists, each is a daily manual upload and self-service onboarding is a slogan. Depends on #2. |

### Tier 1 — a tenant that pays; the books become real; the seven fill in

| # | Work | Id | Why here |
|---|---|---|---|
| 5 | **Subscription billing and the open-core line** | OH-19 | Turns a tenant into revenue, and forces the Apache-2.0 boundary decision that must precede the hosted bank connection (§1.5, §6). Consumes the checklist for the trial-end open item. |
| 6 | **Bank feeds and reconciliation** via Plaid, behind a port — daily pulls, matching against PMS settlements, OTA payouts, payroll debits, vendor payments; unmatched queue; reconciliation status per account per period. | OH-28 | The feature that makes OH the books rather than a report about them, and the daily reason an owner or bookkeeper opens the product. First live test of the E5 amendment, starting with the pilot properties' own operating accounts. Depends on #3, the E5 ADR, Plaid production access (needs the legal entity), and settlement feeds from #1. |
| 7 | **SynXis Property Hub and Visual Matrix parsers** (integrations #4 and #6). | OH-2 | Both stable, both emailed, together 7–8k US properties — the cheapest coverage per parser once #4 exists. Marriott (#5 of seven) waits for the Agilysys Stay report pack to settle, unless a Marriott owner turns up first. Needs sample packs from real properties. |
| 8 | **Mapping editor**, after the dictionary-tenancy decision — base dictionary plus per-org overrides is the recommended shape; `MappingException` is the worklist and `CoveragePage` the read side already. Bundle the `SKYTOUCH` → `CHOICEADVANTAGE` identifier rename here, since both touch the dictionary keying. | OH-20 | Choice codes are franchise-configurable and HotelKey's will be; once OH is the ledger, a wrong mapping is a misposted entry, not a misfiled statistic. Blocked on the schema ADR (§6). |
| 9 | **Receivables and payables, basic** — AR mirrors the PMS city ledger with aging and receipts applied against the bank feed; AP is a vendor master, USALI-coded bill entry and approvals, with OCR capture and bill-pay rails through a connected provider rather than built. | OH-29 | Together they are what an owner means by "my books". AR is small and completes #6's matching; AP keeps the boundary the build-vs-integrate record drew — OH owns the coding and the ledger effect, the capture and money movement are bought. Depends on #3, #6. |
| 10 | **Budget import and variance** | OH-8 | The first column every owner, CPA, and lender reads. Budgets now attach to GL accounts, which is simpler than attaching them to report lines. |
| 11 | **Night-audit repository with sign-off** — every received pack kept (redacted), searchable by business date, with a review record. | OH-24 | Cheap once #2 and #4 exist, and a standing owner request in this category of product. Ranked below the bank feed because that now supplies the daily reason to open OH. |

### Tier 2 — completing the seven and the ledger; the modern stack

| # | Work | Id | Why here |
|---|---|---|---|
| 12 | **OPERA Cloud via OHIP** (Oracle Cloud Marketplace listing) and the **Cloudbeds API** (integrations #3 and #7). | OH-22 | Opera files already work, so OHIP is an upgrade and a channel listing; Cloudbeds is the independent-segment template. Both benefit from #1 having settled the API-source pattern; both need the legal entity for partner agreements. |
| 13 | **Multi-entity books, fixed assets, and year-end** — entities as first-class with intercompany balances and consolidation; asset register, depreciation, FF&E reserve, loan schedules; occupancy/sales tax liability; a CPA export with an accountant role. | OH-30 | Turns "the books for a hotel" into "the books for a hotel company" and earns the CPA's sign-off to leave a general-purpose ledger. Absorbs OH-10 and gives OH-11 an accounting meaning. Test the CPA package with real accountants before building the year-end slice. Depends on #3, #6, #9. |
| 14 | **Notification delivery** — per-tenant recipients and channels (Slack first), suppression-aware. | OH-26 | The delivery plumbing OH-9 and OH-15 lack, and the channel for "three unmatched bank transactions over $500" and "period ready to close". |
| 15 | **Integration marketplace growth** — CRM demand feed, more payroll providers, Yodlee as the second bank adapter. | OH-17 ext. | Each is an adapter behind the existing field-spec pattern and each is a standing maintenance cost: one at a time, mock first, when a real hotel asks. |
| 16 | **Cash forecasting, narrative, chat** — the 13-week cash forecast from AR, AP, payroll runs, and the bank balance; the performance narrative; the conversational interface. | OH-12, OH-21 | With the ledger, bank feed, AR/AP, and payroll in one system, the forecast is a query and the narrative has real material. The standing rule is inherited verbatim: numbers are computed; prose never introduces a figure of its own. |

**Standing work outside the ranking:** observability (OH-3) and CI/CD (OH-4)
remain planned and grow more urgent as tenants become real; neither gates a
specific tier item, so they are scheduled by operational pain rather than
ranked here.

---

## 4. The rename task

`SKYTOUCH` → `CHOICEADVANTAGE` touches the `pms_source` values, the adapter
module names, `mapping/skytouch.yaml`, fixtures, and every test that names
them — thirty-plus files. It is bundled with the mapping-editor schema work
(Tier 1 #8) because both touch dictionary keying, and because a rename that
lands *before* org-scoped overrides exist would have to be redone against the
new keying anyway. Until it lands, docs say choiceADVANTAGE and code says
`SKYTOUCH`; the detection registry is the one place the two meet, and it maps
report headers, not marketing names, so nothing breaks in the interim.

---

## 5. Readiness items that are not features

Becoming the ledger is a liability shift: when OH is an analytics layer, a
mapping bug is an annoyance; when OH is the books, a posting bug is a
misstatement and a bank-token breach is a regulatory event. The engineering
answer is in OH-27 itself (the immutable journal, the audited close, the audit
log). The business answers move earlier than they otherwise would: the legal
entity (also a prerequisite for Plaid production access and the Oracle and
Cloudbeds partner agreements), the SOC 2 timeline, and cyber insurance.

The other readiness test is the CPA. Owners do not pick their ledger; their
accountant does. The year-end package (in OH-30) exists to answer this, and it
should be tested early: ask real owners' CPAs what package they would need to
accept OH's books *before* the posting core is finished. If the honest answer
is "just give me a QuickBooks file", the QBO export stays first-class for a
long time — which it is designed to be either way.

---

## 6. Open decisions

Each should get a decision doc (or a decision line in an existing one) before
the corresponding build starts:

1. **GL posting model** — chart-of-accounts shape (USALI base plus per-org
   extensions), period-close semantics, and the immutability rule. Blocks
   Tier 0 #3 (OH-27).
2. **The E5 amendment** — the aggregator-token posture of §1.2, written as an
   amendment to Pillar E5, including the Plaid-vs-Yodlee coverage check for
   the banks real owners use. Blocks Tier 1 #6 (OH-28).
3. **Pricing basis and the open-core boundary** — what the Apache-2.0 core
   always includes versus what the hosted service charges for. The ledger
   core and the PMS adapters are what make the open core useful; the hosted
   bank connection (aggregator contract, compliance envelope, insurance) is
   the natural paid line. Blocks Tier 1 #5 (OH-19) and must precede #6.
4. **Mapping dictionary tenancy** — org-scoped table, or global base plus
   per-org override layer (recommended)? Blocks Tier 1 #8 (OH-20).
5. **SMS vendor** — required for the verified cell (D-B5) and owner alerting;
   still unchosen. Blocks parts of OH-26.
6. **Whether redaction is destructive** — does `/ingest` redact before
   writing to the inbox, or store raw and redact on promote? D8.4 says "at
   the boundary", which reads as the former. Blocks Tier 0 #2.

Settled since the last revision: per-tenant secret storage (D-OH17.2, ADR-005
field encryption) — and the decisions in §1, each recorded in the positioning
analysis.

---

## 7. Deltas applied to `.github/roadmap.yml` (2026-09-06)

Recorded here because the reasoning lives in this document and the positioning
analysis, not in the yml.

**Entries added:** OH-23 (emailed night-audit intake), OH-24 (night-audit
repository), OH-26 (notification delivery), OH-27 (GL posting core), OH-28
(bank feeds and reconciliation), OH-29 (receivables and payables), all
`planned`; OH-30 (multi-entity, fixed assets, year-end), `considering`. There
is no OH-25: the id was allocated and retired in the analysis that produced
these entries — its capability was folded into OH-28 — and ids are never
reused.

**Entries edited:**

- **OH-2** — now names its targets (SynXis Property Hub, Marriott
  FOSSE/Agilysys Stay, Visual Matrix, Hilton PEP's emailed pack), corrects
  SkyTouch to choiceADVANTAGE and names SkyTouch Hotel OS as a separate
  unbuilt source. Status `considering` → **`planned`**: two of its targets
  are Tier 1.
- **OH-22** — now names its targets (HotelKey, OPERA Cloud via OHIP,
  Cloudbeds).
- **OH-10, OH-11** — each now references OH-30, which absorbs the first and
  gives the second its accounting meaning.
- The header's "every other id is productization" rule widened to cover the
  GL range.

**Not changed:** every shipped status, and OH-19's `planned` status — its
movement is in this document's ordering, where §8 of the previous revision
said such movements belong.

---

## 8. Deliberately not building

Kept explicit so the ledger does not become a general-purpose accounting
product by accretion, and the platform does not drift upstream:

- **A PMS, a booking engine, or a front-desk UI.** The brand systems that
  control franchise properties approve a short list of PMSs, and the
  interface burden of a PMS (locks, payments, phones, POS) is a product in
  itself — none of it where OH's value is. For independents on API-first
  platforms, OH connects; it does not replace.
- **AP capture, OCR, and bill-pay rails.** Bought through a connected
  provider; OH owns the coding and the ledger effect.
- **Payroll tax calculation and filing.** The provider's, per the standing
  Pillar C design.
- **Inventory and cost accounting beyond F&B COGS lines, guest invoicing,
  POS, multi-currency.** The QuickBooks-by-accretion risks.
- **An eighth PMS parser before the seven are done.** Aggregators for the
  tail.
