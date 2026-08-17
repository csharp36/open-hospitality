# Track B / B1 — invite-gated self-service signup (design)

Status: **DESIGN / approved in brainstorm 2026-08-17.** The first buildable
slice of Track B (self-service onboarding, OH-1). Scoping + decisions:
[`2026-08-17-track-b-self-service-onboarding-scoping.md`](2026-08-17-track-b-self-service-onboarding-scoping.md).
Inherits D1 (isolation), D2 (Keycloak Organizations), D8 (always-sensitive
posture + progressive onboarding). Plan follows via writing-plans.

## 1. Goal & principle

Turn the dormant `provision_tenant` primitive into a working, **invite-gated,
server-driven signup**: an approved owner clicks an emailed invite link,
enters the minimum workspace info, verifies their cell by SMS OTP, sets a
password, and lands in the portal as their new tenant's `org_admin` — a real,
always-sensitive tenant with integrations honestly off. No Open Hospitality
staff involved from invite to first login.

## 2. Scope

**In scope (B1 + B3 + a minimal notification/OTP seam):**
1. **Invite gate** — a not-`OrgScoped` `invite` model + service; **CLI** to
   create/approve an invite for an email (pilot); one-time expiring token.
2. **Notification/OTP seam** — a `Notifier` interface (send email, send SMS) +
   a dev **console adapter** + config-selected real adapters (SMTP, one SMS
   vendor); an `OtpService` (generate/verify short codes, hashed, expiring,
   attempt-limited).
3. **Public signup endpoints** — accept-invite, send-OTP, complete-signup.
4. **Server-driven provisioning** — extend `KeycloakAdmin` with set-password;
   `provision_tenant` sets the admin user's password; run it from a tightly
   scoped owner session; mark the invite consumed. Idempotent, fail-closed.
5. **Signup frontend** — a public invite-accept page + workspace form + OTP
   entry, in the app SPA (unauthenticated route, like Track A's `/try`).
6. **The auth handoff** — on success, initiate the standard OIDC login for the
   new credential; the portal's first load is the "confetti" moment.

**Out of scope (deferred):** the full OTP/notification **vendor matrix** (B2);
**tenant lifecycle + per-integration "open items"** state (B4 — the tenant is
created minimal, integrations off); Keycloak-native self-registration (D-B2 —
we're server-driven); a cross-org **platform-admin** HTTP surface (D2 §8 — invite
creation is CLI for the pilot); billing (Track D); the marketing landing / the
Track A→invite approval UI (invite creation is CLI now).

## 3. Decisions locked (from scoping)

Gate = hybrid approve→emailed-link (**D-B4**); server-driven (**D-B2**);
signup-adjacent tables **not `OrgScoped`** (**D-B3**); local password, passkey
later (**D-B5**); notification/OTP seam folds in (**D-B6**); email verified by
the invite click, cell by SMS OTP; always-sensitive from tenant creation (D8).

## 4. Architecture overview

```
  Admin (pilot):  CLI `usali invite <email>`  ──▶ creates invite row + emails link (Notifier)
                                                          │
  Owner (public, unauthenticated):                        ▼
    click link ─▶ GET  accept-invite(token) ─────▶ validates token → returns bound email
    fill form  ─▶ POST send-otp(token, cell) ────▶ OtpService → Notifier.send_sms(code)
    enter code ─▶ POST complete-signup(token, otp, {property, pms, jurisdiction, cell, password})
                        │  validate invite + OTP  (fail-closed)
                        ▼
                 provision_tenant(owner-session)  ──▶ KC org → admin user (+password) → membership
                        │                                → DB organization row → org_admin grant
                        ▼  mark invite consumed (same txn boundary)
                 SPA initiates OIDC login ─▶ portal (org_admin, integrations off) ─▶ 🎉
```

Everything below the public endpoints reuses the **built** engine
(`provision_tenant`, D1 walls, D2 org resolution). The new code is the *surface*.

## 5. Components & boundaries

### 5a. `invite` model + `src/usali/invites.py` — NEW (not `OrgScoped`)
`Invite`: `id`, `email`, `token_hash` (the raw token is a bearer secret, shown
once in the emailed link, stored hashed), `status` (`pending`|`consumed`|`revoked`),
`expires_at`, `created_at`, `consumed_org_id` (nullable, set on consume for
audit). **Not `OrgScoped`** — it precedes any tenant, so it carries no `org_id`
and no RLS policy (D-B3). Service: `create_invite(email) -> (Invite, raw_token)`,
`validate(raw_token) -> Invite | None` (pending + unexpired + hash match),
`consume(invite, org_id)`. Migration adds the table.

### 5b. `src/usali/notifications.py` — NEW (config-selected seam)
`Notifier` protocol: `send_email(to, subject, body)`, `send_sms(to, body)`.
Adapters: `ConsoleNotifier` (logs — dev/test default, no vendor), and
config-selected real ones (SMTP for email, one SMS vendor). Selected in
`create_app` like the payroll/CRM/photo-store seams; tests inject a fake that
captures messages. **`OtpService`** (same module or sibling): `issue(purpose,
target) -> code` (random numeric, stored hashed with `expires_at` + attempt
count in a not-`OrgScoped` `otp_challenge` table or a short-lived store),
`verify(purpose, target, code) -> bool` (constant-time compare, decrement
attempts, fail-closed on expiry/exhaustion). Rate-limited per target.

### 5c. `KeycloakAdmin` set-password — MODIFY `src/usali/keycloak_admin.py`
Add `set_password(user_id, password)` (KC admin REST + the in-memory fake).
`provision_tenant` (`provisioning.py`) gains a `password` param and calls it
after creating the admin user. Nothing else in the chain changes.

### 5d. Public signup router — NEW, ungated (in `server.py`)
Mounted **without** `operator_gates` (like `kiosk_router`). Endpoints:
- `GET /api/signup/invite/{token}` → `{email}` if valid, else 404-style refusal
  (no existence oracle).
- `POST /api/signup/otp` `{token, cell}` → issues + sends the SMS code (invite
  must be valid).
- `POST /api/signup/complete` `{token, otp, property_name, pms_source,
  wage_jurisdiction, cell, password}` → validates invite + OTP (fail-closed) →
  provisions → consumes invite → returns success (the SPA then logs in).
Abuse-guarded exactly like Track A's preview: rate limits, the invite token
required on every call, payload validation. Uses a **dedicated owner-role
session** confined to calling `provision_tenant` + `consume` and nothing else
(the one place a public request touches an owner session — see §7).

### 5e. `usali invite` CLI — NEW (in `cli.py`)
`usali invite <email>` (owner-session, like the seed commands): create the
invite, render the link, and send it via the `Notifier`. The pilot's invite
origination; a GA admin surface replaces it later.

### 5f. Signup frontend — NEW public route (SPA)
An unauthenticated route (allowlisted in `RootShell`, like `/try`): the
invite-accept landing (reads `?token=`), the workspace form (property / PMS
dropdown wired to the `detect` registry / jurisdiction / cell / password), the
OTP entry, and the success → OIDC-login handoff. Warm skin reused from Track A.

## 6. Data flow & errors

Happy path: as the diagram. Failure modes, all **fail-closed**:
- invalid/expired/consumed invite → refusal naming nothing (no oracle);
- OTP wrong/expired/exhausted → refusal + (within limits) re-issue;
- KC or DB failure mid-provision → the transaction rolls back and the invite
  stays `pending` (idempotent re-try safe; `provision_tenant` is find-or-create);
- duplicate signup (invite already consumed) → refusal.

## 7. Security & posture (the sharp edges)

- **Owner session from a public endpoint.** `provision_tenant` requires an
  owner (non-org-instrumented) session. The signup endpoint is the *only*
  public path that touches one, so it is confined to exactly `provision_tenant`
  + `invite.consume` with fully-validated inputs — no other query runs on it.
  This boundary is the review focus.
- **Invite token + OTP code are bearer secrets** — stored hashed, expiring,
  single-use / attempt-limited.
- **Always-sensitive from creation (D8)** — the new tenant is prod-grade the
  instant it exists; integrations are honestly **off**, not mocked.
- **Abuse guards** on every public endpoint (rate limits; invite required).
- **No enumeration oracles** — invalid invite / non-member behave like the
  existing confinement idiom.

## 8. Testing

- **Two-org world** (extend `test_l7_two_org_walk` / `orgworld.py`): a signup
  provisions org N; its admin sees only org N under the real `usali_app` RLS
  role; org N's admin cannot see org 1.
- **Invite lifecycle**: create → valid → consume → invalid-on-reuse; expiry;
  revoke.
- **OTP**: issue/verify happy; wrong/expired/exhausted fail-closed; rate limit.
- **Provisioning**: end-to-end via the fake `KeycloakAdmin` + fake `Notifier`;
  partial-failure rollback leaves the invite `pending`; idempotent re-run.
- **Endpoint security**: the public router opens no org session except the
  confined provisioning one (spy the session factory, à la Track A's
  persist-nothing test); no-oracle refusals.
- **Frontend**: component tests for the form/OTP; an unauthenticated e2e that
  runs a full invite→signup→portal against the real backend + fake vendors.

## 9. Deferred / handoff

B2 (vendor matrix), B4 (tenant lifecycle + per-integration open-items — the
progressive onboarding UX), the Track A→invite approval admin UI, and billing
(Track D) build on this. The `OrgSettings`/`Organization` lifecycle columns
land in B4; B1 creates the tenant minimal.

## 10. Open questions (small, non-blocking)

- OTP store: a DB `otp_challenge` table vs a short-TTL in-process store — DB is
  simplest and testable; decide in the plan.
- Whether `complete-signup` auto-initiates OIDC login or returns to a login
  page — a frontend-plan detail.
