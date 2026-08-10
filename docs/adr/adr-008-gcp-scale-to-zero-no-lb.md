# ADR-008: GCP deployment — scale-to-zero, no load balancer

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

The engine needed a hosted deployment — a reference/demo environment and the basis for a
future hosted product — for a **cost-sensitive** market. The dominant question was
recurring cost at low traffic, where an always-on managed load balancer or per-tenant
compute would swamp the bill.

## Decision

We will deploy on **Google Cloud**, optimized for near-zero idle cost.

- **Two scale-to-zero Cloud Run services** — the application and a **Keycloak** identity
  service — sharing **one Cloud SQL Postgres** instance (separate `usali` and `keycloak`
  databases).
- **Cloud Storage** holds photos as app-side ciphertext (public access prevented);
  **Secret Manager** holds the HPKE key, field-encryption key, and DB credentials.
- **No load balancer.** TLS terminates at the platform; the app opts into
  `--proxy-headers`. Cloud Run reaches Cloud SQL through the **Auth Proxy sidecar** — the
  database has **no public IP**.
- **Serving role ≠ owner:** the app connects as the non-owner `usali_app` role (no
  `BYPASSRLS`, per ADR-002); the schema owner is used only by the migrate/seed job.
- **Deploy ordering is an invariant:** provision infrastructure (which **creates the
  `usali_app` role**) → run migrations + seed as a one-shot job → ship the app revision.
  The RLS migration refuses to run before the role exists; the app revision assumes a
  migrated schema.

## Consequences

- **Idle cost approaches $0** for compute; the always-on Cloud SQL instance is the
  dominant steady-state cost.
- **Cold starts** on the first request after idle — an accepted trade for the demo/early
  hosted tier.
- The ordering invariant is real operational discipline the deploy scripts must enforce.
- Environment-specific values (project id, domains, service-account emails, region,
  bucket names) are **deployment configuration**, kept out of this repository.

## Alternatives considered

- **AWS** — a managed ALB alone would exceed the entire target monthly bill at this
  scale; revisit only on committed credits or a data-residency requirement.
- **Always-on VMs / containers** — rejected: pay for idle capacity a demo doesn't use.
- **A managed load balancer in front of the services** — rejected on cost; platform TLS +
  `--proxy-headers` covers the need without it.
