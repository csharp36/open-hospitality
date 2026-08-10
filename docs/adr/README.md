# Architecture Decision Records

These ADRs capture the major, hard-to-reverse decisions behind Open Hospitality —
the ones a new contributor needs to understand the *why*, not just the *what*. The
format is [MADR](https://adr.github.io/madr/)-lite; see [`adr-000-template.md`](adr-000-template.md).

An ADR is immutable once **Accepted**. To change a decision, add a new ADR that
supersedes the old one and update the old one's status — don't edit history.

| ADR | Decision | Status |
|-----|----------|--------|
| [001](adr-001-usali-local-first-ingestion.md) | USALI standard + local-first PDF ingestion | Accepted |
| [002](adr-002-shared-schema-rls-tenancy.md) | Shared-schema multi-tenancy: Postgres RLS two-wall + composite FKs | Accepted |
| [003](adr-003-keycloak-identity-db-authority.md) | Keycloak identity: one realm + Organizations, authority from DB grants | Accepted |
| [004](adr-004-hpke-client-side-pii-sealing.md) | HPKE client-side PII sealing + blind vault + Opener seam | Accepted |
| [005](adr-005-symmetric-field-encryption-per-org-keys.md) | Symmetric field encryption + per-org photo keys | Accepted |
| [006](adr-006-suppression-disclosure-model.md) | Suppression & disclosure model | Accepted |
| [007](adr-007-self-hosted-biometric-jurisdiction-gating.md) | Self-hosted biometric matching + jurisdiction gating | Accepted |
| [008](adr-008-gcp-scale-to-zero-no-lb.md) | GCP deployment: scale-to-zero, no load balancer | Accepted |
| [009](adr-009-ports-and-adapters-dual-adapters.md) | Ports-and-adapters with dual adapters + a mock per integration | Accepted |
| [010](adr-010-fail-closed-loud-posture.md) | Fail-closed / loud-over-silent posture + three-lens review | Accepted |
