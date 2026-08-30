# Open Hospitality — road to a paying tenant

Status: **GAP ANALYSIS (2026-08-30).** The sequencing narrative for what
stands between today's demo and a hotel that discovers the product, tries it,
signs up, connects its real systems, and pays.

## Relationship to `.github/roadmap.yml`

[`.github/roadmap.yml`](../.github/roadmap.yml) stays the **single source of
truth** the triage bot dedups feature requests against. It is a flat catalogue
of capabilities with stable `OH-<n>` ids. This document is the **ordering and
gap analysis over that catalogue**: what is genuinely missing, which items
silently block others, and what should be built first. Where the two disagree,
the yml wins on *what a capability is*; this doc wins on *what state it is in
and when it should land*. §8 records the deltas already applied back to the yml.

Every claim below is grounded in the code as of 2026-08-30, with the evidence
cited inline. Where a design decision already settles a question, the decision
doc is linked rather than re-argued.

---

## 1. Where we are

**Shipped and load-bearing:**

- The **engine** — detect → parse → stage → promote, USALI mapping, the
  Summary Operating Statement, labor Schedule 14/15, scheduling, the kiosk.
- **Multi-tenancy** — D1 isolation (two walls in `tenancy.py`, 49 `OrgScoped`
  models across 42 tenant tables, RLS fail-closed) and D2 Keycloak
  Organizations identity, both pinned by a real two-org isolation test.
- **Three PMS sources** — Opera, AutoClerk, and SkyTouch (including the
  bundled Standard Audit Pack via `process_pack`), through one detection
  registry.
- **Track A** — the `/try` anonymous parse-preview that persists nothing, live
  on the demo host.
- **Track B / B1** — invite-gated public signup wired to `provision_tenant`,
  first-property creation, PMS-interest capture, SMTP-backed invite and OTP
  email.
- **Property config and core performance statistics** — room inventory,
  fiscal calendar, occupancy / ADR / RevPAR / TRevPAR with comparisons and
  drill-through.

**The shape of what remains:** the product can be *reached* and a tenant can be
*created*, but there is no path by which a stranger discovers it, and no path
by which a created tenant connects its own real systems or pays for the
privilege. Those are the two ends of the funnel, and both are open.

---

## 2. Band 1 — discover → try → sign up

The engine and the on-ramp exist; the front of the funnel does not.

### 1.1 No marketing site (**OH-16**)

`/try` and `/signup` are routes inside the app SPA
(`frontend/src/router.tsx:206`, `:219`) served from the demo host. There is no
positioning page, no pricing page, no "who this is for", nothing indexable by a
search engine. `/try` is the designed aha moment — see
[Track A](design/2026-08-16-track-a-front-door-preview-design.md) — but a
hotel owner who has never heard of USALI has no route *to* it.

What is needed: a public marketing surface that lands a stranger on `/try` and
then on `/signup`, with the pricing story from Band 3 attached to it.

### 1.2 Invite gate → open self-service

Signup is invite-gated by decision (D-B4,
[scoping doc](design/2026-08-17-track-b-self-service-onboarding-scoping.md) §3),
and per [D8](design/2026-08-16-data-posture-progressive-onboarding-design.md)
the pilot gate is a flag lifted at GA, not a second stack. The gate itself is
cheap to lift. What is missing is the **admin surface** around it: invite
creation is deliberately a CLI command (owner-session only), so approving a
Track A capture today means someone shelling into a container.

### 1.3 B2 — the notification seam is half-built

`notifications.py` ships the `Notifier` protocol, a `ConsoleNotifier`, and
`SmtpNotifier`. **`SmtpNotifier.send_sms` raises rather than silently dropping
a code** — an honest refusal, but it means there is no SMS vendor. D-B1 and
D-B5 both assumed a verified cell: for owner alerting (D-B1 rationale (c)) and
as the second factor at signup (D-B5). Email currently carries the whole
delivery story.

---

## 3. Band 2 — try → a real production tenant

This is where the two structural blockers live.

### 2.1 ⚠️ Integration config is process-wide, not per tenant (**OH-17**)

**This is the largest hidden lift on the list.** `config.py:33–50` holds
`qbo_client_id`, `qbo_realm_id`, `qbo_refresh_token`, `payroll_provider`,
`gusto_api_token`, `gusto_company_id`, and the `adp_client_*` pair as
process-wide `Settings` — environment variables, one value for the whole
deployment. Only `crm_provider` was ever made per-org, and it lives on
`OrgSettings` (`models.py:467`), which holds *nothing else*.

The consequence: **"does QBO exist for this tenant?" is currently
unanswerable.** Connecting QuickBooks or Gusto for tenant A would change it for
every tenant. Nothing in the ingestion or reporting path is wrong — the
isolation walls are sound — but the integration layer was built for the
single-org world and never followed D1/D2 into multi-tenancy.

What it requires:

1. Per-tenant credential storage, encrypted at rest (the per-org key machinery
   from [ADR-005](adr/adr-005-symmetric-field-encryption-per-org-keys.md)
   already exists and is the natural home).
2. A per-tenant **OAuth connect flow** for QBO and Gusto — the tenant's own
   consent, their own realm/company id, and QBO's rotating refresh token
   persisted per org rather than per process (`qbo_client.py` already handles
   the rotation correctly; it just has one place to put it).
3. The adapters resolving configuration from the active org instead of `Settings`,
   extending the existing config-selected-seam pattern that `crm_provider`
   already demonstrates per-org.

Three separate features are waiting on this: connect-payroll, connect-QBO, and
the open-items checklist that reports on them.

### 2.2 ⚠️ B4 — the open-items model does not exist (**OH-18**)

[D8.2](design/2026-08-16-data-posture-progressive-onboarding-design.md) is
explicit: there is no sandbox→prod flip; each integration carries its own
lifecycle state per tenant, onboarding is a persisted resumable checklist, and
**"fully prod" is not a state — it is zero open items.** A search of `src/` for
`open_item` or `checklist` returns nothing. `OrgSettings` has no tenant status,
no per-integration state, no checklist.

This is the container that onboarding UI, integration status, alert
configuration, and billing-at-trial-end all hang from. Building it before them
is what keeps them from each inventing their own tenant-state model.

### 2.3 Alerts and notifications as a product feature

`notifications.py` is a transactional seam — invites and OTP codes. There is no
per-tenant recipient configuration, no digest, no delivery preferences. The
roadmap already carries the *content* of alerting as **OH-9** (ledger anomaly
detection, issue #11) and **OH-15** (operational KPI alerts, issue #19); what
neither covers is the **delivery and subscription plumbing** underneath, which
needs the per-tenant state from §2.2 anyway.

### 2.4 Ingestion-boundary redaction is preview-only

D8.4 requires detect-and-redact of guest identity and card PANs **at the
ingestion boundary**, on the grounds that a real night-audit export carries
real guest names and card numbers (observed in a real SkyTouch pack).

Today `redact()` is applied on exactly one path: the anonymous preview
(`server.py:115`, operating on a `PreviewPayload`). The authenticated `/ingest`
endpoint (`server.py:489`) writes the **raw uploaded PDF** to the inbox
directory and moves it to `processed/` — no redaction, and the original bytes
persist on disk.

That is defensible while the only uploader is us. It is **a compliance gate on
the first real tenant uploading a real audit pack**, and it should close before
Band 3 rather than after.

---

## 4. Band 3 — subscription and billing (**OH-19**)

Completely greenfield. A search across `src/` and `frontend/src/` for
`stripe|billing|subscription|plan_tier` returns only Delphi's API
*subscription key*. There is no plan model, no entitlement check, no trial
clock, no metering, and no payment rail.

Until 2026-08-30 `roadmap.yml` mentioned billing only as a trailing clause
inside **OH-1**'s summary, which was not enough to plan against; it is now
**OH-19** in its own right.

D8 already places it correctly: billing is an **open item that becomes required
at trial end** — so it is a consumer of §2.2, not a parallel subsystem.

Decisions not yet made, and each one changes the build:

- **Pricing basis** — per property, per room, per user, or per ingested report.
- **The Apache-2.0 line** — what the open core always includes versus what the
  hosted/premium modules charge for. The LICENSE and README already reserve
  "premium/hosted modules, licensed separately"; nothing defines the boundary.
- **Where the trial clock lives** — tenant state (§2.2) is the obvious host.
- **Entitlement enforcement point** — a gate at the router, at the feature, or
  purely advisory during the pilot.

---

## 5. Band 4 — follow-on capability

### 4.1 ⚠️ The mapping-adjustment UI is blocked on a schema decision (**OH-20**)

`UsaliMappingDictionary` (`models.py:145`) is a plain `Base` model — **not
`OrgScoped`** — keyed `(pms_source, pms_trx_code, usali_edition)` and loaded
from repo YAML by `mapping/loader.py`. It is a single global dictionary shared
by every tenant.

But `mapping/skytouch.yaml` ships entirely `needs-review` *precisely because
SkyTouch transaction codes are franchise-configurable per property*. Those two
facts are in direct conflict, and the conflict surfaces the moment a second
SkyTouch property signs up with a different code set.

A tenant-facing mapping editor therefore requires a decision first: make the
dictionary org-scoped, or add an org-override layer above a shared base. Both
are defensible; neither is free.

The supporting pieces already exist: `MappingException` (`models.py:185`) is
`OrgScoped` and already captures the unmapped rows that would feed a worklist,
and `CoveragePage` already renders coverage gaps — read-only, with no write
endpoint anywhere in `portal_api.py`.

### 4.2 Additional PMS (**OH-2**, `considering`)

- **HotelKey** — intentionally on hold pending API access and a real Final
  Audit Report sample; we don't want to build against one report shape that may
  vary across M3 / Inn-Flow integrations.
- **SkyTouch segmentation** (Revenue by Market / Rate Code) — exists in
  choiceADVANTAGE, not built; must first be enabled in a property's audit pack,
  which makes it an onboarding step as much as a parser.
- **CLI / watcher auto-routing** of a dropped file to pack-vs-single —
  deferred; `process_pack` is a standalone entry point. The property registry
  already records `pms_source`, the intended routing key.

### 4.3 Metrics (**OH-7**, issue #9 — largely shipped)

Small and well-scoped remainders, per
[`reference/performance-metrics.md`](reference/performance-metrics.md) §Deferrals:

- **GOPPAR** and general (non-labor) **CPOR** → blocked on expense ingestion
  (issue #26).
- **Weekly narrative recap** → issue #14 (**OH-12**).

### 4.4 Chat interface over hotel performance (**OH-21**)

Genuinely greenfield — there are zero LLM dependencies in `pyproject.toml` or
`frontend/package.json`. It is also the item that gets *cheaper* the more of
the rest lands, because it wants a clean semantic layer over the SOS,
performance, and labor queries that already exist rather than a new data path.

The standing constraint from **OH-12** applies and should be inherited
verbatim: *numbers are computed; the generator writes prose about them and
never introduces a figure of its own.* That is the same reconciliation
principle D8.3 enforces — an operator compares our output to their own
spreadsheet, so a figure we invented is worse than a blank.

---

## 6. Recommended sequencing

The ordering is driven by what unblocks the most other work, not by what is
most visible.

| # | Work | Why here |
|---|---|---|
| 1 | **B4 — tenant state + open-items model** (§2.2) | The container onboarding UI, integration status, alerting, and billing all hang from. Everything downstream invents its own tenant-state model without it. |
| 2 | **Per-tenant integration config + OAuth connect** (§2.1) | Unblocks connect-payroll, connect-QBO, and the honest "off, not mock" rendering D8.3 requires. The single biggest unlisted lift. |
| 3 | **Ingestion-boundary redaction** (§2.4) | A compliance gate on the first real tenant's first real upload. Cheap now, expensive after. |
| 4 | **Marketing site + open signup** (§1.1, §1.2) | Only worth opening the funnel once a tenant that walks in can reach a working, honestly-labelled portal. |
| 5 | **Billing** (Band 3) | Consumes (1) as an open item; needs (4) to have a pricing page to point at. |
| 6 | **Mapping-editor schema decision, then the UI** (§4.1) | Forced by the second SkyTouch property, not by the first. Decide before building. |
| 7 | Additional PMS, metrics remainders, chat (§4.2–§4.4) | Genuine follow-ons; none block a paying tenant. |

Items 1 and 2 are unglamorous plumbing that three separate user-facing features
are silently waiting on. They are the two things most likely to be
under-scoped if planned from the feature side.

---

## 7. Open decisions

Each of these should get a decision doc (or a decision line in an existing one)
before the corresponding build starts:

1. **Mapping dictionary tenancy** — org-scoped table, or global base plus
   per-org override layer? (§4.1, blocks the mapping UI)
2. **Pricing basis and the open-core boundary** — what the Apache-2.0 core
   always includes. (§4, blocks billing and the pricing page)
3. **Per-tenant secret storage shape** — reuse the per-org field encryption
   from ADR-005, or a dedicated credential store? (§2.1)
4. **SMS vendor** — required for D-B5's verified cell and owner alerting, still
   unchosen. (§1.3)
5. **Whether redaction is destructive** — does `/ingest` redact before writing
   to the inbox, or store raw and redact on promote? D8.4 says "at the
   boundary", which reads as the former. (§2.4)

---

## 8. Deltas applied to `.github/roadmap.yml` (2026-08-30)

The canonical file had drifted from the code. These edits are **applied**, so
the triage bot now dedups against reality. Recorded here because the reasoning
lives in this document, not in the yml.

**Stale entries corrected:**

- **OH-1** — its summary described the model
  [D8](design/2026-08-16-data-posture-progressive-onboarding-design.md)
  retired: *"land in a time-boxed sandbox, and promote to production."* Rewritten
  around reaching a working portal immediately and closing open items
  progressively, with no sandbox tier and no promotion event. Status
  `planned` → **`in-progress`** (Tracks A and B/B1 have shipped).
- **OH-2** — named only Opera and AutoClerk as supported; SkyTouch added.
- **OH-6** — property config, room inventory, and the fiscal calendar have
  shipped. Status `planned` → **`shipped`**.
- **OH-7** — core performance statistics have shipped, but GOPPAR and non-labor
  CPOR are named in its own title and remain deferred to issue #26. Status
  `planned` → **`in-progress`**, with the exception noted inline.

**Capabilities added** — each was genuinely absent, and each is phrased as a
user-facing capability because the bot matches on `summary`:

| id | Capability | Status | §ref |
|---|---|---|---|
| **OH-16** | Public marketing front door | `planned` | §1.1 |
| **OH-17** | Connect your own accounting and payroll accounts | `planned` | §2.1 |
| **OH-18** | Onboarding checklist of open setup items | `in-progress` — backend shipped, frontend pending | §2.2 |
| **OH-19** | Subscription plans and billing | `planned` | §4 |
| **OH-20** | Review and correct USALI transaction-code mapping | `considering` | §4.1 |
| **OH-21** | Conversational interface to hotel performance | `considering` | §4.4 |

None carry an `issue:` field yet — the field is optional, and no tracking issue
exists for them. Add one as each is opened.

**Also updated:** the file's header comment, which claimed `OH-1..OH-5` were the
whole productization roadmap and `OH-6..OH-14` the analytics backlog — already
stale once OH-15 was added, and now covering OH-16..OH-21 as productization.
