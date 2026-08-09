# ADR-005: Symmetric field encryption for compute-on data + per-org photo keys

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

ADR-004 seals store-and-forward secrets so the server can't read them. But some
sensitive values must be **computed on in-process** and therefore cannot be sealed to a
key the server lacks: pay rates and compensation notes (used in payroll math) and
**face-template embeddings** (matching needs the vector in memory). Separately, punch
and face **photos** live in object storage, where database RLS cannot reach.

## Decision

We will use two complementary at-rest mechanisms distinct from the HPKE vault.

- **Symmetric field encryption** — `EncryptedString` / `EncryptedBytes` (AES-256-GCM)
  at rest for compute-on fields (`pay_rate`, `compensation_note`, face embeddings).
  Production **fail-fasts** if it detects the committed dev-default key.
- **Per-org keys for stores outside Postgres** — object keys gain an `org/<id>/` prefix
  **and** the AES-256-GCM data key is **per-org**, HKDF-derived from a master key salted
  by `org_id`. The founding org's key is *defined* as the master key, so existing
  objects stay readable with no re-encryption window.

## Consequences

- The engine can run payroll and match faces while these values stay encrypted at rest.
- A prefix-routing bug in object storage yields **undecryptable bytes**, not another
  tenant's photos — the tenant boundary lives in the ciphertext, not just the path.
- There are now **two crypto regimes** (this and ADR-004); contributors must know which
  applies — the split is deliberate: *store-and-forward* seals to a key the server
  lacks, *compute-on* encrypts with a key the server holds.

## Alternatives considered

- **A single global photo key** — rejected: a routing bug would expose plaintext photos
  across tenants.
- **HPKE-sealing the embeddings** (as in ADR-004) — impossible: matching requires the
  plaintext vector in memory, so a server-unreadable seal cannot work here.
- **Plaintext at rest with access control only** — rejected: a DB or bucket dump would
  expose rates and biometrics directly.
