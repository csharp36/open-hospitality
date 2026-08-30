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
- **Track B / B4** — the onboarding open-items checklist: seven setup items
  probed on read, a permanent `/setup` page, and a sidebar count badge plus a
  first-run dashboard card that retire once nothing is open *and* nothing
  failed to check.
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

### 2.1 Per-tenant integration config (**OH-17**, backend shipped — frontend still missing)

Built to the
[OH-17 design](design/2026-08-30-oh17-per-tenant-integration-config-design.md).
`org_integration_credential` (`OrgIntegrationCredential`, `models.py:467`) is
one `OrgScoped` row per `(org, integration)`: the row IS the connection, so a
tenant cannot hold a provider without credentials for it, and secrets
(`EncryptedString`, per ADR-005) cannot drift from the provider name that
reads them. It absorbed L5's `org_settings.crm_provider` — `OrgSettings` is
now gone entirely, since that column was its only reason to exist. RLS's
`org_wall` policy covers the table like every other tenant table
(`tests/test_l2_rls_wall.py:453`), and a real two-org isolation suite exercises
it through the ORM wall and with the ORM wall bypassed
(`tests/test_integrations.py:452`).

`src/usali/integrations.py` is the registry and the resolution seam:
`resolve_payroll` (returning `ResolvedPayroll`, so a run's provider *name*
travels with its adapter — see the module docstring for the mis-pay this
prevents), `resolve_qbo`, `resolve_crm_feed`, `DbTokenStore`, and the
`IntegrationNotConfigured` / `CredentialUnreadable` refusals ADR-005's
rotation hazard requires. `QboClient` now takes a `TokenStore` instead of a
bare refresh token, so Intuit's per-grant token rotation
(`qbo_client.py:177`) is durable per tenant — closing a bug the client's own
docstring used to document against itself. Every adapter has a real
read-only authenticated `verify()`, so `src/usali/integrations_api.py`'s
`PUT /api/integrations/{integration}` refuses a credential that cannot
authenticate before it is ever stored (D-OH17.8) — connecting is no longer
"paste a key and hope."

`src/usali/integrations_api.py` exposes `GET` / `PUT` / `DELETE
/api/integrations` (org_admin only, no secret ever returned in a response)
plus the QBO OAuth pair (`/api/integrations/accounting/authorize` and its
callback) with an HMAC-signed `state`. One amendment from the design's
original plan: **D-OH17.7 was revised during execution** — `DbTokenStore`
takes no row lock, so concurrent QBO pushes are not serialized, in one
process or across them. Two simultaneous pushes fork the refresh-token
lineage exactly as two workers would; the loser's grant fails visibly and a
retry succeeds. This is accepted, not mitigated — a row lock held across an
outbound HTTP call with no release path on a failed grant was judged worse —
so nothing here should be read as promising cross-process serialization.

**A second accepted residual, decided 2026-08-30:** the OAuth `state` is
signed and short-lived but **not single-use**, so a captured, unexpired state
submitted with an attacker's own fresh Intuit `code` binds the attacker's
QuickBooks company onto the victim org's accounting row. Accepted after
working out that a nonce store would not have closed it — single-use refuses
only the second use, and an attacker who calls back first consumes the nonce
himself. The fix, if it is ever needed, is a browser-bound cookie across the
authorize/callback pair, not a nonce table. Full reasoning in D-OH17.11's
residual-risk block. **This makes one frontend requirement non-optional: the
`/integrations` page must DISPLAY the connected QBO company id**, because the
stored `realm_id` plus the `integration_connected` audit event are the only
signals that separate a hijack from a normal connection.

The open-items checklist (§2.2) is the one place this is fully wired
end-to-end: `payroll`, `accounting`, and `demand_feed` each probe the
tenant's own credential row (`checklist.py`'s `_probe_payroll` /
`_probe_accounting` / `_probe_demand_feed`). Payroll and accounting route to
`/integrations`. The old tripwire that pinned all three to `where: null` is
deleted; its mirror,
`test_demand_feed_is_the_one_item_without_a_surface` (D-OH17.12 as amended by
D-OH17.16), pins the exact set of items with no connect surface, so it fails
in both directions.

**`demand_feed` is deliberately NOT one of them** (decided 2026-08-30). A
credential does not finish that connection: verification and every real pull
need a property `crm_ref`, and the only writer of `crm_ref` is the repo's
YAML seed — no API sets it, `property_config_api` included. Routing it to
`/integrations` would have flipped a checklist item to a form no tenant can
complete, which is the drift OH-17 exists to remove, so it carries an honest
`unavailable_reason` instead. Making `crm_ref` tenant-settable is a feature
of its own (a provider identifier needs validation, a refusal shape, and a
place in property-config to explain itself), not a field to bolt on here.

**What is still not done, and must not be glossed:** the **`/integrations`
frontend page does not exist yet** — it is a separate, not-yet-started plan.
The checklist items above point real operators at a route the SPA does not
serve, so today a click lands on the SPA's not-found page. OH-17 is
backend-complete, not user-complete; nobody can actually connect QuickBooks,
Gusto, ADP, Delphi, or Tripleseat through the product yet, only through
`/api/integrations` directly.

**One** smaller loose end the design doc's §8a carries forward, deliberately:
`cli.py`'s `_qbo_client_from_settings` (`cli.py:549`) still builds its
`QboClient` from process-wide `Settings` with a `StaticTokenStore` — the CLI
is not org-aware at all, acceptable while it is an operator tool run against
one deployment, but it should not grow a second user. (The sibling hazard
§8a once carried beside it — `payroll_run_api.create_run` recording
`provider_name` from `Settings` instead of the resolved row — was fixed
before merge in `resolve_payroll`/`ResolvedPayroll`, and §8a was rewritten to
say so: it now opens "Resolved 2026-08-30 in `986b5da`". Nothing is stale
there; the correction has landed.)

### 2.2 B4 — the open-items model (**OH-18**, shipped)

[D8.2](design/2026-08-16-data-posture-progressive-onboarding-design.md) is
explicit: there is no sandbox→prod flip; each integration carries its own
lifecycle state per tenant, onboarding is a persisted resumable checklist, and
**"fully prod" is not a state — it is zero open items.** When this document was
written, a search of `src/` for `open_item` or `checklist` returned nothing.

It exists now, backend and frontend, built to the
[B4 design](design/2026-08-30-track-b-b4-open-items-checklist-design.md).
`checklist.py` holds a closed registry of seven items, each owning a probe;
status is **derived on read**, never stored (D-B4.1), so the only persisted
rows are dismissals (`org_checklist_override`) — a stored `payroll: connected`
would outlive the credential being revoked, the exact drift D8.3 forbids. There
is still no tenant status column and no lifecycle enum on `organization`, also
deliberately (D-B4.7): "fully prod" stays the derived predicate `all_clear`,
which is what keeps D8's retired promotion model from growing back as a column.
The frontend renders it on three surfaces sharing one query key, so they cannot
disagree: `/setup`, which is permanent, and a sidebar count badge and a
first-run dashboard card, which both retire on `all_clear` rather than on
`open_count == 0`. The distinction matters exactly once — a total probe failure
leaves `open_count` at zero while nothing is actually known, and gating on it
would retire both surfaces at the moment the operator most needs them.

What shipped is the **container**, not its consumers. Onboarding UI beyond
`/setup`, per-integration connect surfaces (§2.1), alert configuration (§2.3)
and billing-at-trial-end (§4) still hang from it and are still open; building it
first is what keeps each of them from inventing its own tenant-state model.

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
| 1 | **B4 — tenant state + open-items model** (§2.2) — **shipped** | The container onboarding UI, integration status, alerting, and billing all hang from. Everything downstream invents its own tenant-state model without it. |
| 2 | **Per-tenant integration config + OAuth connect** (§2.1) — **backend shipped** | Unblocks connect-payroll, connect-QBO, and the honest "off, not mock" rendering D8.3 requires. The checklist already routes to `/integrations`; that page itself is the remaining, still-unplanned frontend slice. |
| 3 | **Ingestion-boundary redaction** (§2.4) | A compliance gate on the first real tenant's first real upload. Cheap now, expensive after. |
| 4 | **Marketing site + open signup** (§1.1, §1.2) | Only worth opening the funnel once a tenant that walks in can reach a working, honestly-labelled portal. |
| 5 | **Billing** (Band 3) | Consumes (1) as an open item; needs (4) to have a pricing page to point at. |
| 6 | **Mapping-editor schema decision, then the UI** (§4.1) | Forced by the second SkyTouch property, not by the first. Decide before building. |
| 7 | Additional PMS, metrics remainders, chat (§4.2–§4.4) | Genuine follow-ons; none block a paying tenant. |

Items 1 and 2 are unglamorous plumbing that three separate user-facing features
are silently waiting on, and the two most likely to be under-scoped if planned
from the feature side. (1) has landed, and so has (2)'s backend; the
under-scoping risk that motivated calling it out has now moved onto the
`/integrations` frontend slice specifically, which is still unplanned.

---

## 7. Open decisions

Each of these should get a decision doc (or a decision line in an existing one)
before the corresponding build starts:

1. **Mapping dictionary tenancy** — org-scoped table, or global base plus
   per-org override layer? (§4.1, blocks the mapping UI)
2. **Pricing basis and the open-core boundary** — what the Apache-2.0 core
   always includes. (§4, blocks billing and the pricing page)
3. ~~**Per-tenant secret storage shape**~~ — **settled** by D-OH17.2 in the
   [OH-17 design](design/2026-08-30-oh17-per-tenant-integration-config-design.md):
   ADR-005 symmetric field encryption, not a dedicated credential store or
   ADR-004's blind vault — Intuit's server-side QBO token rotation needs a
   write-back path a blind vault cannot offer. (§2.1)
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
| **OH-18** | Onboarding checklist of open setup items | `shipped` | §2.2 |
| **OH-19** | Subscription plans and billing | `planned` | §4 |
| **OH-20** | Review and correct USALI transaction-code mapping | `considering` | §4.1 |
| **OH-21** | Conversational interface to hotel performance | `considering` | §4.4 |

None carry an `issue:` field yet — the field is optional, and no tracking issue
exists for them. Add one as each is opened.

**OH-18 drifted, and is now resynced.** It moved `planned` → `in-progress` in
this document at `c154d45` without the yml following, so the catalogue read it
as unstarted for two commits. Both now say `shipped`. Worth naming because §8
claims the two are kept in sync: nothing enforces that, so a status edit here
is only half an edit.

**Also updated:** the file's header comment, which claimed `OH-1..OH-5` were the
whole productization roadmap and `OH-6..OH-14` the analytics backlog — already
stale once OH-15 was added, and now covering OH-16..OH-21 as productization.
