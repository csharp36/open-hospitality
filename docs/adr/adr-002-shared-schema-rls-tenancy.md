# ADR-002: Shared-schema multi-tenancy — Postgres RLS two-wall + composite FKs

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

The V1 engine ran as a single organization. Self-service onboarding turns that
single-org trust boundary into a **security boundary between untrusted strangers** —
one tenant must never see, reference, or corrupt another's data. The isolation has to
be **fail-closed** (a mistake yields no data, not the wrong data) and it must not
depend on every request handler remembering to filter by tenant.

## Decision

We will use **shared-schema tenancy**: an `org_id` column on every tenant-owned table
(44 tables), protected by **two independent walls that share one predicate**, plus
composite foreign keys on the sensitive spine.

- **Application wall** — a SQLAlchemy session hook adds `org_id = current_org` to every
  ORM SELECT that touches a tenant-owned table. A handler that forgets to filter is
  filtered anyway; a session with **no** org context refuses loudly rather than
  returning an unscoped result.
- **Database wall** — per-table **Row-Level Security** with `FORCE ROW LEVEL SECURITY`,
  keyed on a transaction-local session variable (`app.org_id`). Serving paths connect
  as a **non-owner role (`usali_app`, no `BYPASSRLS`)**, so even raw SQL is fenced. An
  unset variable evaluates to `NULL` → **zero rows**, never all rows.
- **One predicate feeds both walls**, so they can only disagree by one being dropped —
  and each drop is pinned by a test the *other* wall cannot mask.
- **Composite `(org_id, x_id)` foreign keys** on references that carry money or PII,
  because Postgres runs FK checks with **owner** privileges and RLS cannot stop a buggy
  write from pointing at another org's parent row. Config/lineage references keep
  single-column FKs; each exception is reasoned in the migration.

## Consequences

- **Either wall alone stops a leak** — defense in depth against a single-layer bug.
- **One Postgres instance** serves all tenants: provisioning a tenant is inserting
  rows, not standing up infrastructure.
- The default is fail-closed: an unbound session sees nothing. This trades a class of
  "empty result vs. forbidden" ambiguity (the 403-vs-404 question) for safety — a
  known, deliberate product wrinkle, not a bug.
- The composite-FK spine is verbose and must be maintained as new tables land.

## Alternatives considered

- **Instance-per-tenant** — strongest isolation, but a per-tenant always-on database
  cost that does not scale down; rejected on economics.
- **Database-per-tenant** — cheaper than instances but still N databases to migrate and
  monitor; rejected.
- **Schema-per-tenant** — rejected on operational failure modes: per-schema Alembic
  drift, DDL-shaped provisioning, and a stale `search_path` that can leak an entire
  tenant at once (a worse blast radius than a row filter).
