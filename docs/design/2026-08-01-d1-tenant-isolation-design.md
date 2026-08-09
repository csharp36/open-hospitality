# D1 — Tenant isolation model: shared schema + org_id + Postgres RLS

Status: **DECIDED 2026-08-01** (user). Resolves fork D1 of the
onboarding-flow design (`2026-08-01-onboarding-flow-design.md` §11).
Not a plan — the multi-tenancy phase plan follows once D2
(Keycloak identity/tenancy) is also resolved.

## The decision

One shared Cloud SQL instance, one shared schema. Every tenant-owned
table carries `org_id`, and isolation is enforced by **two independent
walls** — application-level automatic query scoping and database-level
Row-Level Security — so a cross-tenant leak requires two simultaneous
failures. Sandbox and prod tenants use the same mechanism.

## Constraints that forced it (from the D1 discussion, 2026-08-01)

- **Tenant profile: self-service strangers, dozens+ in year one.**
  Open signup from the marketing site; isolation must assume hostile
  co-tenants; per-sandbox marginal cost must be near zero.
- **Prod cost: near-zero marginal cost there too.** Paying tenants —
  who hold real employee PII and biometric photos — also share
  infrastructure. Isolation comes from the schema/application layer,
  not from buying separate boxes.
- These two answers eliminate instance-per-tenant outright (~$10/mo
  per tenant just for SQL) and database-per-tenant on the shared
  db-f1-micro (per-DB connection memory, migrations × N).
- Schema-per-tenant was rejected on operational failure modes at
  self-service scale: alembic must run once per schema (deploys slow
  with tenant count; a partial failure strands tenants on divergent
  schema versions — a drift class the single-head verification cannot
  see), provisioning becomes 47-table DDL instead of an INSERT, and
  the residual failure mode — a stale `search_path` on a pooled
  connection — leaks an entire tenant at once, where the RLS
  equivalent (unset session var) fails to empty.

## 1. The two walls

**Application wall.** A SQLAlchemy `do_orm_execute` hook on the
session adds `org_id = :current_org` criteria to every SELECT against
org-scoped models (`with_loader_criteria` over an `OrgScoped` mixin).
No per-endpoint discipline required: a handler that forgets to filter
still gets filtered. The existing `assignment_scope` property
confinement is untouched — it remains the *intra*-org layer; the org
wall sits below it.

**Database wall.** RLS policies on every tenant-owned table:
`USING (org_id = current_setting('app.org_id', true)::int)`. The app
opens each transaction with `SET LOCAL app.org_id = <n>`. An unset
variable makes `current_setting(..., true)` return NULL and the
policy yield **zero rows — fail-closed**. The app connects as a
dedicated non-owner role with no `BYPASSRLS`; every tenant table gets
`FORCE ROW LEVEL SECURITY` so even owner-role code paths cannot skip
it. Alembic runs as the owner role. `CREATE ROLE` is cluster-level,
so the second SQL role lands in `bootstrap.sh`, not a migration.

## 2. Schema change

`org_id` is denormalized onto every tenant-owned table (~40 of 47;
today only `property` carries it — everything else reaches the org
through one join). The plan enumerates the exclusions: only
`alembic_version` and any table that is genuinely org-independent
reference data; when in doubt a table is tenant-owned. One migration: add column → backfill to org 1 (all
existing data is org 1 by construction — A2.1 seeds a single default
org) → `NOT NULL` → index. `organization` itself gets an RLS policy
(a tenant sees only its own org row).

**Known subtlety, handled explicitly: FK checks bypass RLS.** A buggy
write could reference another org's parent row even though it cannot
read it. Where a cross-org reference would carry money or PII (e.g.
pay-run lines → employee), the FK becomes composite
`(org_id, x_id)` so the schema itself refuses. Which FKs get this
treatment is judged per-table in the plan.

## 3. Org resolution

The request's org comes from the authenticated principal. The exact
mechanism (token claim vs DB lookup) is **D2's call** — this design
only requires that auth hands the session one trusted `org_id` before
any query runs. Until D2 lands, a DB lookup from the principal works
and is swappable.

## 4. The stores outside Postgres

- **GCS photos**: per-org object prefix, and the AES-256-GCM data key
  becomes per-org (HKDF-derived from the master key + org_id) — a
  prefix-routing bug yields ciphertext that does not decrypt, not
  another hotel's punch photos.
- **Per-org config**: `Settings` fields like `crm_provider` are
  process-wide today; integration selection moves to an `org_settings`
  table (part of the phase; shape decided in the plan).
- **Sandbox expiry (D7)**: deletion is `DELETE WHERE org_id` plus a
  GCS prefix delete — cheap by construction under this model.

## 5. What this buys later

Everything keyed by `org_id` means a future big customer relocates to
a dedicated instance via `pg_dump` filtered by org. The isolation
mechanism does not hard-wire the deployment shape, even though we are
not paying for dedicated boxes today.

## 6. Testing posture

The suite already runs against real Postgres 16 (testcontainers), so
RLS is testable in the normal pin suite — no second test lane.
Two-org pin worlds throughout (the two-employee/two-run idiom
promoted to tenancy):

- cross-org reads return empty;
- unset session var returns empty (the fail-closed pin);
- a raw-SQL path with the ORM hook bypassed still returns empty
  (proving the DB wall stands alone);
- the FK-bypass world pins the composite-FK refusal.

Mutation targets: policy dropped, `SET LOCAL` dropped, ORM criterion
dropped, backfill wrong, `FORCE RLS` removed.

## 7. Deliberately out of scope

D2 (Keycloak realm/group model), the provisioning flow
(build-sequence step 2), billing/metering (D4), and the re-audit of
every suppression/disclosure gate in a two-org world — that last is
the phase's adversarial-review lens, not a design-time item.
