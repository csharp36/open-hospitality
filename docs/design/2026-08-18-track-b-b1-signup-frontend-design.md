# Track B / B1 — signup frontend + first-property wiring (design)

Status: **DESIGN (2026-08-18).** Part-2 of Track B/B1. Part-1 (the backend HTTP
surface + CLI) shipped as PR #65 on `feat/onboarding-track-b`; this branch,
`feat/track-b-b1-signup-frontend`, is cut off it. Implements design §5f of
[`2026-08-17-track-b-b1-invite-gated-signup-design.md`](2026-08-17-track-b-b1-invite-gated-signup-design.md)
and closes its two open questions (OTP store already settled in Part-1; the
post-signup handoff is settled here).

## 1. Goal

Turn the built, invite-gated signup **API** into something an approved hotel
owner can actually use: a public SPA that accepts an invite, verifies the cell
by SMS OTP, sets a password, names the workspace and its first property, and
lands the owner in the portal — with the property created "integration off"
(D8). Signup that names an **unsupported PMS** still creates the workspace and
routes a de-duped demand signal to an admin instead of dropping it.

## 2. Scope

**In:** the public `/signup` SPA (two-screen wizard + success → OIDC handoff);
the backend wiring that makes the already-declared `property_name` /
`pms_source` / `wage_jurisdiction` fields real (create the first property); the
unsupported-PMS capture → de-dupe → admin-route flow.

**Out (deferred):** the warm Track-A "front door" skin (Track A is unmerged;
Part-2 uses the existing app design system, warm skin is later polish); PMS
credential/data connection and room-inventory / fiscal-calendar setup (existing
post-login PropertyConfigPage, D8 progressive onboarding); fuzzy/alias PMS
matching; a demand-report CLI (optional follow-up).

## 3. Decisions locked (from the 2026-08-18 brainstorm)

- **D-F1 Signup collects and creates the first property.** `provision_tenant`
  makes only org + admin; a separate step creates the property. The backend
  starts *using* `CompleteRequest`'s existing `property_name` / `pms_source` /
  `wage_jurisdiction` fields (no schema change except D-F4).
- **D-F2 Two-screen wizard.** Screen 1 (accept invite + cell → send code) maps
  to `POST /otp`; Screen 2 (code + workspace/property/PMS/jurisdiction/password
  → create) maps to `POST /complete`; then a success screen.
- **D-F3 Handoff = success screen → OIDC login.** On `201`, show "workspace
  ready" with a CTA that starts OIDC login with `login_hint=<email>`; the owner
  enters the password they just set and lands in the portal.
- **D-F4 Timezone auto-detected.** Add an *optional* `timezone` to
  `CompleteRequest`; the SPA sends the browser zone
  (`Intl.DateTimeFormat().resolvedOptions().timeZone`); server falls back to the
  `Property.timezone` default (`America/Los_Angeles`). Business-date attribution
  is correct from day one; the owner can change it later.
- **D-F5 Existing design system.** Build with the app's current tokens /
  components; no dependency on unmerged Track A.
- **D-F6 Unsupported PMS still creates the workspace** (org + admin, no
  property) and records a de-duped interest request routed to an admin. Not a
  block — consistent with D8 "integrations honestly off".

## 4. Architecture

### The two-session constraint (load-bearing)

`provision_tenant` runs on the **least-privilege provisioner session** (D-B7),
which is granted only `organization` + `role_assignment` and **cannot write
`property`** (a tenant-data table). So the first property is created on a
**separate app-role session bound to the new `org_id`**, writing under the new
org's RLS — after the provisioner commit. The provisioner session's confinement
(opened once, runs only `provision_tenant`) is unchanged.

```
POST /api/signup/complete
  step 1  APP session      validate invite + verify OTP + atomic claim   (Part-1)
  step 2  PROVISIONER      provision_tenant -> org + admin  (only place opened)
  step 2b APP (org-bound)  supported PMS -> create first property
                           OR  pms_source == "other" -> record PMS interest
  step 3  APP session      mark invite consumed_org_id                    (Part-1)
  -> 201 { org_alias }
```

Two commits (provisioner org+admin, then app-role property) are inherent to the
least-privilege split. **Failure posture:** if step 2b fails after a successful
provision, the workspace exists without a property — a legitimate D8 state; the
owner adds the property post-login via PropertyConfigPage. Realistically only a
`property_id` PK collision can fail 2b, and that is retried (see §5).

## 5. Backend changes

### 5a. `CompleteRequest` (`signup_api.py`) — MODIFY

- Constrain `pms_source` to `{"opera", "autoclerk", "other"}`.
- Add optional `pms_other_name: str | None` (required, 1–60 chars, when
  `pms_source == "other"`; rejected otherwise — a 422).
- Add optional `timezone: str | None` (D-F4; IANA string, coarse length cap;
  the property default fills a null).

### 5b. First-property creation — NEW `create_first_property(...)` in `mapping/property_registry.py`

`create_first_property(session, org_id, *, name, pms_source, wage_jurisdiction, timezone) -> str`
inserts one `Property` under the org-bound session and returns the generated
`property_id`. `property_id` is a **global** PK (`String(50)`), so it is
generated as `slugify(name)[:40] + "-" + <4 hex>` and **retried on the rare
unique collision** (a few attempts, then loud failure). `timezone` is omitted
(server default fills it) when null. Created "integration off": no room
inventory, fiscal calendar, or PMS credentials — those are post-login.

`/complete` step 2b calls it inside `with app_bound_session(result.org_id)`
(an app-role session with the new org bound via `bind_org_context`). On success,
continue to step 3.

### 5c. PMS-interest capture — NEW table + service + migration

- **`pms_interest_request`** (plain `Base`, **not `OrgScoped`** — platform-level
  demand data read across orgs by an admin, same rationale as `invite`/`otp`).
  Migration `b1d0pmsinterest` (head `b1c0otp → b1d0pmsinterest`; update the
  single head-literal test). Columns: `id` PK, `org_id` (nullable FK →
  `organization.org_id`, for reference), `email`, `raw_pms` (what they typed),
  `normalized_pms` (dedupe key), `status` (`server_default 'new'`),
  `created_at`. `UNIQUE(org_id, normalized_pms)`.
- **`pms_interest.py`**: `record_request(session, *, org_id, email, raw_pms) ->
  tuple[PmsInterestRequest, bool]`. Normalizes `raw_pms` →
  `normalized_pms` = lowercased, non-alphanumerics stripped (so "HotelKey",
  "hotel key", "Hotelkey" collapse). Upserts `ON CONFLICT (org_id,
  normalized_pms) DO NOTHING`; the bool is `is_new`. Does not commit (caller owns
  the txn). Fuzzy/alias matching is a documented later refinement, not built now.
- **Routing to admin:** in `/complete`, when `pms_source == "other"`, step 2b
  becomes `record_request(...)` on the app-role session (the table is
  not-`OrgScoped`, so no org context needed). If `is_new`, the request's
  `Notifier` sends `settings.admin_notify_email` a summary ("Org {alias}
  ({email}) requested PMS: {raw_pms}"). Notify only on `is_new` so duplicates
  don't spam. New config `admin_notify_email: str = ""` (empty → the
  `ConsoleNotifier` logs it in dev; a real address in prod).

### 5d. `/complete` handler — MODIFY

After the provisioner commit (step 2), branch on `pms_source`:
- supported → `create_first_property(...)` on the org-bound app session;
- `"other"` → `record_request(...)` (+ admin notify on new) on the app session.
Then step 3 (mark invite) and `201`. The response gains `pms_supported: bool`
so the SPA can render the right success copy.

## 6. Frontend

### 6a. Route & shell

Add `/signup` to `RootShell`'s **unguarded** allowlist (the existing mechanism
that renders `/kiosk` and `/callback` as a bare `<Outlet/>`, bypassing
`RequireAuth`). The route reads `?token=` from the URL.

### 6b. `SignupPage` — a 3-state machine (`cell → details → done`)

- **On mount:** `GET /api/signup/invite/{token}` → show the invited email. A
  `404` renders **one generic "this invite link isn't valid or has expired"**
  message and **no form** — fail-closed, no existence oracle (unknown / expired /
  consumed / revoked are indistinguishable).
- **Screen 1 (`cell`):** mobile input (E.164-ish) → `POST /api/signup/otp` →
  `204` advances; `429` → back-off copy; `404` → the generic message.
- **Screen 2 (`details`):** OTP code · workspace name (auto-slugs an **editable**
  `workspace_alias`, validated against `^[a-z0-9][a-z0-9-]{1,62}$`) · property
  name · **PMS dropdown (Opera, AutoClerk, "Other — my PMS isn't listed")** —
  "Other" reveals a required free-text "Which PMS do you use?" · **jurisdiction
  dropdown (US-CA, US-FL)** · password (≥ 8) · hidden browser-detected timezone.
  Submit → `POST /api/signup/complete` → `201` advances; **`403` → inline "code
  incorrect or expired" + a resend affordance** (re-calls `/otp`); `422` → field
  errors; `429` → back-off; `404` → generic.
- **Screen 3 (`done`):** copy keyed on `pms_supported` — supported → "Your
  workspace and {property} are ready"; unsupported → "Your workspace is ready.
  We don't support {PMS} yet — we've logged it and will email {email} when it's
  live." Both show a CTA that starts OIDC login via the existing `login()` with
  `login_hint=<invited email>`.

### 6c. API module `src/api/signup.ts`

`getInvite(token)`, `requestOtp(token, cell)`, `completeSignup(payload)` — plain
`fetch`, **no auth header** (public endpoints). Client-side validation mirrors
the server (alias regex, password length, required fields, "other" requires a
name) to fail fast; the server remains the source of truth.

## 7. Data flow & errors (all fail-closed)

- invalid / expired / consumed / revoked invite → identical generic refusal, no
  form, no oracle;
- OTP wrong / expired / exhausted → `403`, inline retry + resend within limits;
- rate-limited (`429`) on `/otp` or `/complete` → back-off copy, no detail leak;
- provision failure → Part-1 reverts the claim (invite stays retryable);
- property-creation failure after provision → workspace exists property-less;
  the owner completes setup post-login (D8);
- unsupported PMS → workspace created, demand captured + routed, owner informed.

## 8. Testing

- **Backend:** extend `tests/test_b1_signup_api.py` — supported PMS creates a
  `Property` under the new org with the right `pms_source` / `wage_jurisdiction`
  / timezone and a unique `property_id`; `"other"` creates **no** property but a
  de-duped `pms_interest_request` and (on new) an admin notification; the
  `property_id` collision retry; timezone default when omitted. Unit-test
  `create_first_property` and `pms_interest.record_request` (normalize +
  de-dupe). All Part-1 `/complete` invariants (confinement, fail-closed, claim
  race) still pass.
- **Frontend:** vitest component tests for `SignupPage` — each step, the
  fail-closed invalid-invite copy, auto-slug, the "Other PMS" reveal + required
  name, dropdowns, `403` resend, and the two success variants.
- **e2e:** if a Playwright harness exists on this branch, an unauthenticated
  walk against the real backend (seed an invite via the CLI, complete signup,
  assert the property/interest row). If no harness is present, cover the flow
  with the backend integration tests + component tests and **note the gap** —
  do not port Track A's harness onto this branch.

## 9. Deferred / follow-ups

Warm Track-A skin (post Track-A merge); PMS credential/data connection + room
inventory + fiscal calendar (post-login, D8); fuzzy/alias PMS de-dupe; a
`usali pms-requests` demand-report CLI; promoting `/signup` polish once Track A's
front door lands.
