# D2 — Identity/tenancy in Keycloak: one realm + Organizations

Status: **DECIDED 2026-08-01** (user). Resolves fork D2 of the
onboarding-flow design (`2026-08-01-onboarding-flow-design.md` §11).
Companion to D1 (`2026-08-01-d1-tenant-isolation-design.md`) — this
doc fills D1 §3's deliberately-open seam: where the trusted `org_id`
that binds `SET LOCAL app.org_id` comes from. Not a plan — with D1
and D2 both decided, the multi-tenancy phase plan can now be written.

## The decision

One Keycloak realm (`usali`, unchanged), with each tenant as a
**Keycloak Organization** (first-class in KC 26, the exact image K3
deploys). Users are members of one or many organizations; a protocol
mapper puts the membership list into every access token; the server
validates a per-request active org against that list and only the
validated org binds the D1 session variable. Authorization becomes
org-scoped DB grants — realm roles alone never grant authority.

## Constraints that forced it (from the D2 discussion, 2026-08-01)

- **Multi-org accounts in v0** (user): one human — e.g. a management
  company's accountant running the books for two unrelated hotel
  groups — signs in once and works in both tenants. This alone kills
  realm-per-tenant (N realms = N separate accounts).
- **Shared login page accepted for v0** (user): every tenant signs in
  at the shared auth host under Open Hospitality branding; no per-tenant
  login theming.
- **The K3 invariants are load-bearing**: one fixed issuer
  (`…/realms/usali` — chosen so tokens never change issuer), one
  audience, one JWKS URL, and the SPA authority baked into the bundle
  at build time. Realm-per-tenant breaks all four and fights the
  realm-import-is-first-boot-only deployment shape; dozens of realms
  also bloat a scale-to-zero Keycloak's cold starts.
- **Fallback kept in reserve**: plain groups + a custom
  group-membership mapper can produce the same claim shape. The app
  consumes only "a trusted membership claim + a validated active
  org," so Organizations ↔ groups is swappable if the newer feature
  proves rough. The claim SHAPE is the contract, not the KC feature.

## 1. Tenant ↔ Organization mapping

Each tenant is a KC Organization; the KC org **alias** is the join
key. The `organization` table gains a unique `kc_org_alias` column.
The token claim carries aliases only; the app resolves
alias → `org_id` from the database. **The database stays the source
of truth for org identity** — nothing numeric from the token is ever
trusted as an org_id.

## 2. Token contract

The realm gains KC's organization protocol mapper; every access token
carries the user's org memberships as a claim. `auth.py` parses it
into `Principal.org_aliases` (frozenset) with the existing
claim-shape discipline — malformed shapes refuse via `_malformed`,
exactly like `realm_access` today. The claim shape is pinned in
`tests/test_oidc_realm_contract.py` beside the audience-mapper pin.

## 3. Active-org selection

The SPA names the active org per request via an `X-Active-Org` header
carrying the alias. The server validates **active ∈ token
memberships**; only then does the resolved org bind D1's
`SET LOCAL app.org_id`. Fail-closed rules:

- unknown or non-member alias → 403 naming nothing (the existing
  confinement idiom — no existence oracle);
- multi-org token with no header → 400 (ambiguity refuses, never
  guesses);
- single-org token with no header → defaults to its one org.

The org picker/switcher is SPA-side UX over the same claim.

## 4. Org-scoped authorization — the role model change

Realm roles remain the *vocabulary* (`org_admin`, `accountant`,
`property_gm`, `department_manager`, `payroll_admin`, `employee`) and
the coarse operator/employee gate, but **role authority moves to
org-scoped DB grants**. Today `org_admin`/`payroll_admin`/
`accountant` are honored from the token alone (realm-global) — in a
multi-org world that is a cross-tenant privilege leak: an org_admin
of tenant A would carry the role in a token used while active in
tenant B.

Fix: `role_assignment` (already keyed by `keycloak_subject`, already
org-owned under D1) becomes the source of truth for which roles a
subject holds in which org — org-wide roles as rows with no property
scope; property/department roles as today. Effective roles for a
request = roles granted by assignment rows **visible under the active
org's RLS**. The org_admin-of-A-active-in-B world resolves to no
authority by construction, because B's RLS shows none of A's rows.
Every `require_*` gate shifts from token-roles-only to
DB-backed-per-org — the meaty part of the phase, and the only shape
that does not leak.

## 5. Provisioning

`KeycloakAdmin` grows `create_organization`, `add_member`, and an org
lookup. Signup becomes: create KC org → create first user →
membership → DB `organization` row + org_admin grant — all API calls,
no DDL, matching D1's INSERT-shaped provisioning. The dev realm JSON
gains a dev organization with the personas as members;
`make_cloud_realm.py` carries the orgs config through the dev→cloud
derivation (pinned like everything else it carries); runtime-only
configuration goes through `configure_auth.py`.

## 6. Unchanged invariants

One issuer, one audience, one JWKS URL, the baked SPA authority,
PKCE, the Open Hospitality login theme, the `usali-api` audience
mapper. None move.

## 7. Testing posture

- Claim-shape pins in the realm contract test.
- Active-org refusal pins: non-member alias 403, ambiguous multi-org
  400, single-org default.
- The marquee pin — the cross-tenant role world: org_admin of A,
  active in B, refused on every org-gated surface.

Mutation targets: membership check dropped, default-org resolution
wrong, role-grant intersection dropped (token roles honored alone
again), alias→org_id resolution trusting a token value.

## 8. Deliberately out of scope

Per-org IdP federation (the "we sign in with our Entra ID" story —
Organizations makes it an org-level config later, nothing built now);
branded per-org login (v0 decision above); org-switcher UX polish;
platform-admin tooling across orgs (needs its own explicitly
cross-tenant role, designed with the provisioning phase).
