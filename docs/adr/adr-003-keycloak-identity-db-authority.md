# ADR-003: Keycloak identity — one realm + Organizations, authority from DB grants

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

The system needs an OIDC identity provider that supports multi-tenant accounts (one
person may belong to several hotel groups). The subtlety: **realm roles are
realm-global**. A token minted for an `org_admin` of tenant A carries that role no
matter which tenant the request is acting in — so roles alone cannot decide authority
in a multi-tenant world.

## Decision

We will run **one Keycloak realm** with a single issuer, audience, and JWKS URL (all
load-bearing invariants), and use **Keycloak 26 Organizations** for membership.

- The token carries organization **aliases** (never numeric ids) as the membership
  claim, plus coarse realm roles and an optional `(property_id, department_id)` scope
  list. Malformed claims refuse by naming only the claim, never its value.
- **Realm roles gate only the coarse door** (operator vs. employee).
- **Per-request authority is DB grants** — `role_assignment` rows read *under the active
  org's RLS*. An org-wide grant is a row with `property_id = NULL`.
- **Active org is named per request** via an `X-Active-Org` header. The server validates
  `active ∈ memberships`, then resolves the alias to an `org_id` through the
  `organization` table. **Nothing numeric from the token is ever treated as an org_id.**

## Consequences

- **`org_admin` of A, active in B, is refused on every org-gated surface** — the realm
  claim and the DB grant must intersect, and a stale grant can never out-rank the coarse
  claim.
- A single realm keeps cold-starts small and lets one account span tenants.
- The **claim shape is the contract**, not the Keycloak feature — Organizations could be
  swapped for groups + a custom mapper without touching the resource server.
- Authority now requires a DB read per request (under RLS), not just token inspection —
  a deliberate cost for correctness.

## Alternatives considered

- **Realm-per-tenant** — rejected: breaks the single issuer/audience/JWKS invariants,
  makes multi-org accounts impossible, and multiplies cold-start cost.
- **Honoring realm roles from the token directly** — rejected: it is exactly the
  cross-tenant privilege leak this decision exists to prevent.
