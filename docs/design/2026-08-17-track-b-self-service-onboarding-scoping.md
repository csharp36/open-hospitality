# Track B — self-service onboarding: scoping & decisions

Status: **SCOPING / thinking doc (2026-08-17).** The onboarding milestone's
tenancy gate (OH-1). Builds on the decided architecture — **D1** (tenant
isolation, `2026-08-01-d1-tenant-isolation-design.md`) and **D2** (Keycloak
Organizations, `2026-08-01-d2-keycloak-tenancy-design.md`) — and on **D8**
(`2026-08-16-data-posture-progressive-onboarding-design.md`, the
always-sensitive posture + progressive per-integration onboarding + the
public-preview-vs-invite-gated-prod env topology). The B1 slice gets its own
spec once this scoping converges.

## 1. The gap: the engine is built; the self-service surface is not

A codebase survey (2026-08-17) found the multi-tenant **engine** substantially
built and pinned, and only the self-service **surface** missing.

**BUILT (evidence in `src/usali/`):**
- **D1 isolation** — two walls in `tenancy.py`: the ORM read wall
  (`do_orm_execute` + `with_loader_criteria(OrgScoped)`), the DB wall
  (`SET LOCAL app.org_id` + Postgres RLS, fail-closed on unset var, non-owner
  `usali_app` role), and the write wall (`before_flush` org stamping). **49
  `OrgScoped` models, 42 tenant tables** carry `org_id`; migrations `l1–l5`.
  Pinned by a real **two-org RLS isolation test** (`test_l7_two_org_walk.py`).
- **D2 identity** — truly multi-org: KC Organizations, `organization` token
  claim → `Principal.org_aliases`, `require_active_org` + alias→org_id DB
  resolution, and **role authority via org-scoped DB grants** (`require_grants`).
- **`provision_tenant()`** (`provisioning.py`) — the full KC-org → admin-user →
  membership → DB-org → `org_admin`-grant chain, idempotent, owner-session-only.
  **But it is never called at runtime — only from tests.**

**MISSING (the Track B work):**
1. No public **signup** endpoint (every router is operator-gated; `provision_tenant`
   is an unwired primitive).
2. No **OTP / email / SMS** verification (no facade; Keycloak has no SMTP / `verifyEmail`).
3. No **invite-gate / allowlist** (nothing restricts who can create a tenant).
4. Keycloak **self-registration is OFF**.
5. No **tenant lifecycle / per-integration state** (`OrgSettings` holds only
   `crm_provider`; no status, no time-box, no mock-vs-real "open items").

## 2. Decomposition

| Slice | What | Depends on |
|---|---|---|
| **B1** Public signup → provision | wire `provision_tenant` behind a public endpoint; visitor becomes `org_admin` | B3 (gate), B2 (verify) |
| **B2** OTP facade | configurable email/SMS verification (1–2 vendors + self-hostable) | — |
| **B3** Invite-gate / allowlist | restrict who may sign up (pilot); consumes Track A "early access" captures | — |
| **B4** Tenant lifecycle + per-integration state | status + the progressive mock→real "open items" (D8) | B1 |
| **B5** KC self-registration + SMTP config | realm config for registration + email | supports B1/B2 |

**First slice: B1 + B3** — the invite-gated, server-driven signup that turns the
dormant `provision_tenant` into the real pilot on-ramp. B2 and B4 layer on next.

## 3. Decisions log

- **D-B1 — Local accounts baseline; social login deferred (2026-08-17).** Baseline
  on **Keycloak-managed local accounts + our OTP (email AND SMS)**, not social
  ("Sign in with Google/Microsoft"). Rationale: (a) Keycloak already IS the
  account-maintenance system, so local accounts add little burden; (b) social
  fights the self-hostability value (external IdP dependency + OAuth-app
  registration); (c) social is email-identity only — the **cell** we need for
  owner *alerting* still requires SMS OTP regardless; (d) the server-driven
  signup→`provision_tenant` transaction is cleanest with local accounts (social
  brokering would push org-provisioning into a KC first-broker-login hook).
  Social login stays a **later, optional Keycloak realm config** — D2's contract
  is the token *claim shape*, not the IdP, so the app is already
  provider-agnostic and adding it is zero app code.

- **D-B2 — Signup is server-driven (CONFIRMED 2026-08-17).** The app
  orchestrates KC user creation via the admin API (as `provision_tenant`
  already does), keeping invite-gate + OTP + provisioning in one controlled
  flow; NOT Keycloak-native self-registration.

- **D-B3 — Signup-adjacent tables live OUTSIDE RLS (CONFIRMED 2026-08-17).**
  Lead-capture, the invite allowlist, and any pre-tenant rows are
  **org-independent** (no org exists yet), so they are **NOT `OrgScoped`** and
  carry no RLS policy — resolving the "non-tenant rows under RLS" question
  deferred from Track A's lead-capture.

- **D-B4 — Invite-gate = hybrid approve→emailed-link (C) (CONFIRMED 2026-08-17).**
  Admin approves an email (Track A capture or manual) → system emails that
  address a one-time expiring invite link → clicking it opens signup with the
  email pre-bound and already verified. Threads the Track A funnel into the gate
  and is the GA-ready shape. For the pilot, **invite creation is a CLI command**
  (owner-session), so we need no cross-org platform-admin HTTP surface yet
  (D2 §8 keeps that out of scope); public signup *consumes* the invite.

- **D-B5 — Credential = local password (CONFIRMED 2026-08-17).** Owner sets a
  password at signup; passkey/passwordless is a later Keycloak option. Email
  verified by the invite click; the **cell** verified by SMS OTP (and captured
  for owner alerting).

- **D-B6 — Minimal notification/OTP seam folds into B1 (CONFIRMED 2026-08-17).**
  Rather than build B2 first, B1 ships the `Notifier`/OTP **interface** + a dev
  **console adapter** (logs the link/code — testable with no vendor) + a config
  path for real providers (SMTP + one SMS vendor). The full vendor matrix stays
  B2. Follows the existing config-selected-seam pattern (payroll/CRM/photo-store).

**First spec:** `2026-08-17-track-b-b1-invite-gated-signup-design.md`.
