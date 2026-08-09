# ADR-007: Self-hosted biometric matching + jurisdiction gating

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

Face matching at the time clock touches **biometric-privacy law**, which varies sharply
by jurisdiction (Illinois BIPA's private right of action and statutory damages, CCPA/CPRA,
notice-and-retention regimes elsewhere). Getting this wrong is a litigation exposure, not
a bug ticket. It also involves the most sensitive data the system handles — a face
template — which we would rather not ship to a third party.

## Decision

We will do biometric matching **self-hosted and in-process**, gated by a fail-closed
jurisdiction posture.

- **Models:** self-hosted SCRFD (detect) + ArcFace (embed), **CPU, in-process**,
  sha256-pinned and **never committed** (fetched at deploy).
- **Jurisdiction posture table** gates enablement and **fails closed**: only the encoded
  jurisdiction (notice-at-collection, ≤30-day retention) enables; every other
  jurisdiction — **including a bare, unspecified "US"** — refuses to enable.
- **Matching gates approval, never the punch.** Wage law requires recording the punch;
  a face mismatch flags for review, it does not deny time.
- **A human approves every red/grey outcome** — a defense against automated-decision
  liability.
- Embeddings are encrypted at rest per ADR-005.

## Consequences

- **No third-party biometric processor** and no egress of face data.
- Enabling a new jurisdiction is a **deliberate, reviewed act** — adding a row to the
  posture table — not a config toggle.
- Matching runs on CPU in-process, which bounds throughput but keeps the deployment
  simple and the data local.

## Alternatives considered

- **A third-party face-matching API** — rejected as the default: it fits the BIPA-suit
  pattern, carries statutory ambiguity, and exports the most sensitive data we hold. Kept
  only as a possible fallback behind the same posture gate.
- **Punch-photo-as-evidence with no templates** — the conservative pre-go posture: store
  the photo for human review, run no biometric match, until a jurisdiction is explicitly
  enabled.
