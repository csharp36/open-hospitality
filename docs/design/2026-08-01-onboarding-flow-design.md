# Onboarding Flow — design (v0 scoping draft)

Status: **DRAFT / thinking doc.** Captures the vision (user, 2026-08-01),
structures it into a tenant lifecycle, and flags the decisions that
gate the build. Not a plan — the plan doc follows once the forks below
are resolved. The PMS-variant matrix (§4) is pending a research pass.

## 1. Purpose & principle

An arbitrary hotel operator finds us, signs up, sets up, and starts
paying — **fully self-service, with nobody at Open Hospitality involved
from discovery through setup to billing.** Onboarding is the machine
that turns an anonymous visitor into a paying, wired-up tenant.

- **Customer service is a separate track and OUT of scope here.** Support,
  success, and human-assisted setup are a different surface; onboarding
  must stand on its own without them.
- **Progressive disclosure:** collect the *minimum* info at each step. No
  form asks for anything the current step doesn't need.
- **Fictitious-by-construction carries forward:** the sandbox is the
  cloud demo's posture per-tenant — mock integrations, no real
  compensation/PII — until an explicit promotion to prod.

## 2. The spine: a tenant's lifecycle

```
Visitor ──CTA──▶ Sandbox tenant ──promote──▶ Prod tenant
(marketing)      (time-boxed,               (real integrations,
                  mock-wired,                 billing, real PII)
                  synthetic/own-file data)
```

The key reuse: **the Pillar K cloud demo already IS a working
self-service sandbox** — Cloud Run + Keycloak + mock providers
(qbo/gusto/adp/delphi/tripleseat) + a synthetic seed. Onboarding
productizes it into a per-tenant, time-boxed instance. Promotion to
prod is a **config flip from mock adapters to real ones**, not a
re-implementation (the app already selects providers by config, e.g.
`USALI_CRM_PROVIDER=delphi|""`).

## 3. Stage-by-stage flow (minimum info per step)

| Step | Collects | Produces |
|---|---|---|
| **0. Marketing site + CTA** | nothing (email capture optional) | a click to "Start free — no card" |
| **1. Create workspace** | work email, property name, **PMS type** (§4), country/state (wage jurisdiction) | a **sandbox tenant** + the first admin user (Keycloak self-registration → reuse `onboarding.py` for role assignment) |
| **2. Connect your PMS** *(the main touchpoint)* | file-upload **or** API choice; a sample night-audit/trial-balance | a validated ingest — the operator sees their own report mapped to USALI lines (the "aha") |
| **3. Delivery + alerts** *(see §5)* | expected reports, cadence/cutoff, delivery method, who to alert | a per-property delivery contract + alert config |
| **4. Invite the team** | teammate emails + roles (GM, payroll, accountant) | the user set, via the existing operator-onboarding service |
| **5. Promote to prod** *(see §6)* | billing method, real integration auth (payroll, QBO, bank) | a live tenant; mock→real seam flipped; real-PII boundary crossed here |

## 4. The PMS integration touchpoint

The one place the product meets the outside world. Two modalities:
**file/report export** (today: PDF via the `detect` registry) and
**API**. We want **placeholders for every known PMS variant** so an
operator always finds their system in the list — even before the
adapter is built (a "request this PMS" / "upload a sample" fallback
keeps the funnel from dead-ending).

- Extend the `detect` registry `(header-string → (pms_source,
  report_type))` and the `adaptors/` set. A placeholder = a registered
  variant with an "adapter not yet built — send us a sample" path.

### 4a. The strategic pattern (from the 2026-08-01 research)

**File-upload is the universal, zero-dependency self-service on-ramp.**
The catalog splits every PMS into two integration modalities, and this
maps directly onto our self-service goal:

- **File/report export** — works for *any* PMS whose operator can export
  a night-audit / trial-balance / manager report. Crucially it needs
  **no vendor approval, no API tier, no partner agreement** — the
  operator downloads their report and uploads it. This is the same
  ingest model we already run (Opera/AutoClerk PDF). It is the honest
  default for "no one at OH *or the PMS vendor* is involved."
- **API pull** — the automation upgrade: OAuth + scheduled pulls, no
  manual upload. But API access is often gated (Oracle OHIP isn't a
  cold anonymous signup; Cloudbeds' API can be a paid add-on; RMS
  activation goes through a support mailbox). So API is a *premium /
  later* path per PMS, not the on-ramp.

Implication for the touchpoint (§3 step 2): lead with **upload a
sample report** (universal), offer **API connect** where the PMS
supports self-serve keys (Mews, Apaleo, Clock, and the paid-tier ones).
Two adapter families: **file-parse** (extends today's architecture) and
**API-pull** (new).

### 4b. Build priority (file-parse first — matches current architecture)

Already built: **Oracle OPERA**, **AutoClerk** (+ **Agilysys** Visual
One/Stay/LMS share AutoClerk's parent — likely near-term).

**File-export variants to build next** (cheap — reuse the PDF/report
ingest): roomMaster (InnQuest), WebRezPro, eZee Absolute, Hotelogix,
RDPWin (Resort Data Processing), Clock PMS+, ASI FrontDesk, Stayntouch,
**Visual Matrix** (large US economy-motel base), innRoad + ThinkReservations
(no API at all — file is the *only* path, matches AutoClerk exactly).

**API-first variants** (new adapter family, best self-serve dev UX):
**Apaleo** (API *is* the product, free registration), **Mews** (free
public sandbox), **Cloudbeds** (largest US independent base — but API
may be a paid tier), plus protel/Planet, Infor HMS, HotelKey (rising
fast on the Hilton migration), SkyTouch/choiceADVANTAGE (Choice-brand
density).

**Placeholders cover the full catalog (~30)** so every operator finds
their PMS; unbuilt ones route to "upload a sample / request this PMS."

**Brand-mandated systems target the underlying engine, not the brand:**
Choice → SkyTouch, Hilton → HotelKey, Marriott → Agilysys/legacy,
Wyndham full-service → Oracle. Those franchisees can't swap PMS but
still want our labor/USALI layer — the file-upload path serves them
regardless of API gating.

### 4c. Integration standards

- **HTNG Express PMS API** (OpenTravel-schema-based, lightweight) is the
  most promising "one adapter, many PMS" bet — Visual Matrix is the
  first implementer — but adoption is early and vendor-by-vendor, so
  it's a *watch/target*, not a foundation. (No current "HTNG NightAudit"
  standard exists — treat any such reference as superseded.)
- **Hapi** (commercial PMS-agnostic connectivity layer; Maestro,
  HotelKey, Shiji) is a paid shortcut to multi-PMS API coverage — worth
  evaluating vs. building API adapters one-by-one.

Full research — the 30-variant matrix, modality grouping, top-15 build
list, standards, and sources — lives in
[`docs/reference/pms-variants.md`](../reference/pms-variants.md).

## 5. File-delivery monitoring & alerting

Once a PMS is connected, the GM must hear about it when the daily data
doesn't arrive clean. The pipeline **already detects** three of the
four cases; the new work is the *expectation* and the *channel*.

| Alert | Already detected? | New work |
|---|---|---|
| **Didn't arrive** | ❌ no | an **expectation model**: per property, the expected report set + cadence + cutoff time; a daily check fires if a file is missing by cutoff |
| **Corrupt / unparseable** | ✅ `ingestion.py` quarantines a `failed` batch with the error | wire the failure to the alert channel |
| **Duplicate** | ✅ file-hash idempotence (re-ingest no-ops) | surface "you sent this file twice" instead of silently no-oping |
| **Wrong property** | ✅ `detect` rejects header-vs-registration mismatch | wire the rejection to the alert channel |

- **Alert config is captured at PMS onboarding** (§3 step 3): which
  reports, what cutoff, who to notify (GM email/SMS), which channel.
- **Disclosure guard:** alert bodies are operational metadata only
  (file / property / status / time) — **never** figures, employee
  names, or CRM labels. The standing suppression rule applies verbatim.
- Consider a per-property **"all clear" / daily digest** so silence
  isn't ambiguous (no news could mean healthy or mean the monitor died).

## 6. Sandbox → prod promotion (the mock→real seam)

"Minimize any manual work." The seam already exists as config-selected
providers; promotion is a guided flip per integration:

- **Payroll:** mock gusto/adp → real provider via OAuth.
- **Accounting:** mock qbo → real QuickBooks via the existing OAuth (P8).
- **CRM/demand:** mock delphi/tripleseat → real, or leave off.
- **Data:** synthetic year → the tenant's real ingested reports.
- **Billing:** off → active.

Promotion **gates**: each named integration validated, billing active,
and an explicit acknowledgement at the **fictitious→real data boundary**
(the moment real revenue + employee PII enter). Everything provable
before the flip lives in the sandbox; the flip itself should be a
button, not a ticket.

## 7. Billing, bank, QuickBooks (prod)

- **Subscription billing** (e.g. Stripe): the customer enters payment;
  **we never touch raw card/bank data** — the processor does (same
  principle as the rest of the system's credential posture).
- **Bank connection:** clarify scope — we are the labor/USALI layer, not
  a money-mover. Payroll *funding* is the payroll provider's job
  (Gusto/ADP), and QBO reconciles. "Connect bank" almost certainly
  means connecting the **provider**, not us holding funds — otherwise
  we take on payroll-processor licensing. **Decision D5.**
- **QBO push:** the P8 integration already exists; promotion turns it on.

## 8. Creating the user set

Reuse the existing operator-onboarding service (`onboarding.py`): it
provisions Keycloak users + authoritative `role_assignment` rows for
operator roles (GM, payroll, accountant), and records hourly employees
passwordless for the kiosk. Onboarding step 4 drives it per invited
teammate. The open question is *where* those users live in Keycloak
under multi-tenancy (§10, D2).

## 9. Data API + API keys

**Recommendation: yes, build a read API, and issue keys at setup.**
Rationale: (a) it's real customer value — the operator pulls their own
USALI facts / labor / payroll data into their BI or systems; (b) it
generalizes the integration story (inbound files + outbound API); (c)
it's a premium-tier lever for the open-core model.

- **API keys** minted at setup (and rotatable): generated server-side,
  **shown once, stored hashed** (secret-handling posture), scoped to the
  tenant + read/write scopes.
- Start **read-only** (report/fact retrieval); write endpoints later.
- Same auth boundary discipline as the app: tenant-scoped, audited.

## 10. Load-bearing prerequisite: multi-tenancy & isolation

Self-service onboarding **cannot ship without multi-tenancy.** The
`organization` table exists, but the runtime is single-org and today's
suppression/PII confinement is reasoned within *one org's trust
boundary*. Self-service turns that boundary into a **security boundary
between untrusted strangers.** This is the milestone's multi-tenancy
lift, pulled forward as a hard dependency. Forks in §11 (D1, D2).

## 11. Open decisions (the forks that gate the plan)

- **D1 — Tenant isolation model.** ✅ DECIDED 2026-08-01: shared
  instance + shared schema, `org_id` on every tenant-owned table,
  enforced by two walls — automatic ORM query scoping AND Postgres
  RLS (fail-closed on an unset session var). Same mechanism for
  sandbox and prod (self-service strangers at dozens+ scale forced
  near-zero marginal cost for both). Full design + rejected
  alternatives: `2026-08-01-d1-tenant-isolation-design.md`.
- **D2 — Identity/tenancy in Keycloak.** ✅ DECIDED 2026-08-01: one
  realm + Keycloak Organizations (KC 26 native) — tenant = org,
  multi-org accounts in v0, membership claim in the token, active org
  validated per request (that validated org binds D1's session var),
  role authority moved to org-scoped DB grants (realm roles alone
  never grant authority — closes the cross-tenant org_admin leak).
  Realm-per-tenant rejected (breaks the fixed-issuer/baked-authority
  invariants; kills multi-org accounts). Full design:
  `2026-08-01-d2-keycloak-tenancy-design.md`.
- **D3 — Sandbox data.** Pure synthetic (like the demo) vs let the
  operator upload their **own real PMS reports** for the ingest "aha."
  The reports we ingest are *financial* (revenue, ledgers, occupancy) —
  employee PII enters separately via the roster — so the sandbox could
  accept real report uploads without touching PII. Worth allowing for
  the "aha", with the roster/PII staying a prod-promotion step.
- **D4 — Billing model.** Per-property? per-employee? flat tier? Drives
  metering and the Stripe design. (Business decision.)
- **D5 — Money movement.** Confirm we integrate a payroll provider (who
  funds) rather than becoming a processor (licensing). Shapes "connect
  bank."
- **D6 — API scope.** Read-only first vs read-write; which resources.
- **D7 — Sandbox time-box.** Length, and expiry behavior (convert /
  freeze / delete). GDPR-style deletion on abandon.

## 12. Explicitly out of scope

Customer service / success / human-assisted setup (separate track);
channel-manager/OTA inbound; the marketing site's *content* (this doc
covers only its CTA→signup seam); pricing strategy (D4 is the business
call, not this doc).

## 13. Rough build sequence (sketch, post-decision)

1. **Multi-tenancy foundation** (D1/D2) — provisioning, isolation,
   org-scoped everything. The gate.
2. **Sandbox provisioning** — per-tenant time-boxed demo instance
   (productize Pillar K), synthetic seed, mock-wired.
3. **Signup + marketing CTA seam** — steps 0-1, min-info, user set.
4. **PMS connect + placeholders** — extend detect/adaptors from the
   research matrix; the upload-a-sample fallback.
5. **Delivery monitoring + alerts** — expectation model + GM channel.
6. **Promotion seam** — mock→real flips, billing, gates.
7. **Data API + keys.**

Customer-service tooling, advanced billing, and the long tail of PMS
adapters follow.
