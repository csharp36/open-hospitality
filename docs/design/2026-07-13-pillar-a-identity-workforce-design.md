# Pillar A — Identity & Workforce Management Design

**Date:** 2026-07-13
**Status:** Approved for planning
**Depends on:** P0–P9 (merged; FastAPI + React portal, PMS→USALI pipeline, QBO push)

## Context: the larger thesis

The guiding thought experiment is *"what would it take to build an AI-assisted, modern-stack
competitor to Inn-Flow and M3"* — hospitality back-office platforms that unify accounting,
labor management, and payroll. open-hospitality today is the **financial/accounting pillar** of
that vision (PMS → USALI facts → CPA pack / QBO). This spec begins a second pillar —
**workforce & payroll** — whose payoff loop closes back on the first: labor cost will flow as
**Schedule 14 (Payroll Related Expenses) and Schedule 15 (Payroll/FTE)** facts into the P&L
the engine already produces.

That pillar decomposes into three subsystems plus cross-cutting concerns:

- **A — Identity & workforce management** (this spec)
- **B — Time & attendance** (timecards, scheduling, hours → labor cost) — later
- **C — Payroll processing** (an *integration* to embedded payroll rails, not an in-house
  payroll engine) — later
- Cross-cutting: compliance & PII security; the payroll-provider abstraction.

**Locked platform decision:** payroll rails are **bought, not built** — Pillar C will build the
UX + labor-intelligence layer on embedded payroll infrastructure (e.g. Check, Gusto Embedded,
Finch, or ADP APIs), which owns gross-to-net, tax filing, ACH, and garnishment *execution*.
This spec must therefore establish identity, the workforce model, and PII-grade access control
so that C plugs in without reworking the foundation.

## Goal

Give the platform authenticated identity for two user populations, a relational workforce model
tied to USALI schedules, and department-scoped access control with segregated payroll-PII
access — and retrofit the existing portal behind that auth. No money movement, no tax/bank data
(those are Pillar C); this pillar makes the ground safe for them.

## Decisions locked with the user

1. **Two populations, both authenticate.** Operators (GM, managers, accountants, admins) use
   the full portal; employees get self-service (mobile clock-in later, schedule, pay stubs).
   The employee is a first-class identity from day one.

   > **Post-approval note (2026-07-13), from pilot GM ground-truth:** at the pilot the real
   > Inn-Flow time-capture flow uses **no employee PIN/password** — an employee taps their
   > profile on a shared iPad **Time Clock** kiosk, takes a **live identity photo**, and picks
   > Clock In / Start Lunch / End Lunch / Clock Out; the punch + photo are stored for manager
   > verification, then approved timecards feed Inn-Flow Payroll. This refines the employee
   > half of this decision: the employee "identity" is likely a **managed record + photo-punch
   > kiosk** for time capture (a Pillar B cornerstone), with any self-service (pay stubs) via a
   > lightweight path rather than passwords. **A1 (operator auth) is unaffected;** the employee
   > auth mechanism is resolved in A2/Pillar B. The `employee` realm role and `employee-portal`
   > client A1 creates are provisional pending that decision.
2. **Self-hosted Keycloak (OIDC)** for both populations — no per-MAU cost at hourly-employee
   scale, full control of worker PII, standard OIDC/JWT consumed by FastAPI + React. Raw
   hand-rolled auth is explicitly rejected.
3. **Department-scoped RBAC matrix** with a **segregated Payroll Admin role** as the only role
   that can read sensitive PII (segregation of duties).
4. **Retrofit auth onto the entire platform** — the existing SOS/coverage/upload/reports/QBO
   surfaces move behind Keycloak too. One authenticated system; no open door beside payroll PII.
5. **Properties migrate fully to the DB** — the org model becomes the system of record and
   `detect.py` reads the detection registry from the DB; `properties.yaml` is retired.
6. **One spec, two implementation phases** (A1 auth foundation, A2 workforce model).

## Architecture

- **Keycloak** as a new `docker-compose` service, one realm, two OIDC clients (operator portal,
  employee self-service). Roles and property/department scope travel in token claims
  (groups/attributes).
- **FastAPI as an OIDC resource server**: validate Keycloak JWTs against JWKS, extract roles +
  scope, enforce per-endpoint via a shared dependency. Python-stack OIDC (authlib / python-jose),
  not the Java/Spring path. Default-deny.
- **React**: OIDC Authorization Code + PKCE. The existing portal gains a login/session shell; a
  new (initially minimal) employee self-service surface is added.
- **App is system of record** for the HR/employee record; Keycloak holds credentials only. On
  hire the app provisions a Keycloak user via the admin API; on termination it disables that user.
- **Deployment isolation:** the workforce schema and (future) PII live behind the resource-server
  gate in the same platform, now uniformly authenticated.

## Data model (Alembic migration)

- `organization` — the hotel group.
- `property` — becomes the org system of record (migrated off `properties.yaml`).
- `property_detection_alias` — `(property_id, pms_source, match_phrase)`; carries what the YAML
  `match`/`pms_source` rows held so `detect.py` can resolve a PDF's property from the DB.
- `department` — carries `usali_schedule_id` + `usali_edition` (**the labor-cost seam** to
  Schedule 14/15).
- `position` — job/title; holds FLSA exempt/non-exempt classification.
- `employee` — links 1:1 to a Keycloak subject id; home property + department; pay type
  (hourly/salary); hire/term dates; manager. **No SSN/bank/garnishment fields yet** — those are
  Pillar C; this table is designed so they slot into the encrypted-field pattern below.
- `role_assignment` — `(keycloak_subject, role, property_id, department_id?)`. **The app DB is
  authoritative** for role/scope assignments (consistent with "app is system of record"); the
  app writes them into Keycloak so they appear as JWT claims. The claims are used for request-time
  enforcement; this table is the source they are derived from and the basis for query filtering.
  Single source of truth — no Keycloak-vs-DB drift.
- `audit_event` — who viewed/changed which workforce record, when.

## RBAC

- **Roles:** Org Admin; Payroll Admin (only role that can read sensitive PII); Property GM;
  Department Manager; Accountant/Finance (financial pillar + labor-cost reports, *not* PII);
  Employee (self only).
- **Scope:** each operator assignment is scoped to a property and optionally a department
  (the matrix). Department Manager → one department; GM → whole property; regional → multiple
  assignments; Employee → self.
- **Enforcement:** Keycloak claims → a FastAPI dependency resolves the caller's allowed
  (property, department) set and filters every query; default-deny. Existing financial endpoints
  require Accountant/Finance or above.

## Key flows

- **Operator login:** OIDC → JWT (roles + scope) → portal renders per permissions.
- **Employee onboarding:** operator creates the employee record → app provisions a Keycloak user
  → email/SMS invite → employee sets credential → self-service. Termination disables the Keycloak
  user and marks the record.
- **Existing-portal retrofit:** SOS/coverage/upload/reports/QBO endpoints move behind the
  resource-server dependency; the React portal gains the login shell.
- **Detection after migration:** `detect.py` resolves property via `property_detection_alias`
  rows instead of `properties.yaml` (behavior-preserving; the same match phrases, now in the DB).

## Security & PII

- **Field-level encryption pattern** (AES-GCM via an app-layer SQLAlchemy converter, keys held
  outside the DB) defined now for the sensitive columns Pillar C will add — so C adds fields, not
  protection.
- Every (future) PII read gated behind the Payroll Admin check; segregation of duties enforced.
- `audit_event` records access to workforce and (future) PII records.
- Matches the project's "build it right from day one" and HIPAA-grade crypto posture.

## Testing & gates

- **Keycloak Testcontainer** (or a mock JWT issuer) alongside the existing Postgres Testcontainer.
- Assert: default-deny on unauthenticated requests; scope filtering (a HISJ GM cannot read SSSJ
  data); department-manager isolation; Payroll-Admin-only PII gate (using a placeholder protected
  field until C adds real ones); detection still resolves properties after the YAML→DB migration.
- Existing gates unchanged: strict mypy (`packages=["usali"]`), ruff, pytest; frontend tsc/oxlint/
  vitest/Playwright. The financial suites must stay green through the retrofit.

## Implementation phases (one spec, planned as two)

- **A1 — Auth foundation:** Keycloak service + realm/clients; FastAPI resource server; retrofit
  the existing portal + endpoints behind login; core operator roles; React login shell. Ships a
  secured platform.
- **A2 — Workforce model:** org/property/department/position/employee entities; properties→DB
  migration + `detect.py` cutover; department-scoped RBAC matrix; segregated Payroll Admin role;
  employee onboarding/provisioning; employee self-service shell; encrypted-field pattern +
  audit log.

## Out of scope (explicit)

- Timecards, scheduling, hours→labor-cost (Pillar B).
- SSN, bank/direct-deposit, garnishments, tax withholding, pay runs, payroll-provider integration
  (Pillar C) — only the *pattern* and the Payroll Admin role are established here.
- The actual Schedule 14/15 reporting rollup (arrives once B produces labor cost).
- Multi-org SaaS tenancy beyond the single pilot hotel group.

## Definition of done

Both populations authenticate via Keycloak; the existing portal is behind login with financial
endpoints gated to Accountant/Finance and above; the workforce model exists with departments
carrying USALI-schedule ids; properties are the DB system of record and detection reads from the
DB with the financial suites still green; department-scoped RBAC enforces property/department
isolation with a Payroll-Admin-only PII gate proven by test; onboarding provisions and
deprovisions Keycloak users; the encrypted-field pattern and audit log are in place for Pillar C;
all gates green.
