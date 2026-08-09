# ADR-010: Fail-closed / loud-over-silent posture + three-lens review

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

This is a financial and PII system. In this domain a **silent wrong answer** — a
mis-scoped query, a leaked rate, a partial load booked as complete — is far more
dangerous than a loud failure, because it is trusted and acted upon. The other ADRs
(RLS fail-closed default, prod key refusals, quarantine-on-error) all lean on this
being a shared, explicit stance rather than an accident of each author's taste.

## Decision

We will adopt **fail-closed, loud-over-silent** as a cross-cutting posture, and gate
every pillar through **three independent adversarial review lenses** before merge.

- **Fail closed:** a session with no org context refuses (`MissingOrgContext`); a
  cross-org write raises (`OrgContextMismatch`); an unset RLS variable yields **zero**
  rows; production refuses committed dev-default keys; a bad ingest file **quarantines**
  and records a failed batch rather than loading partially.
- **Loud over silent:** the system prefers a visible refusal to a plausible-looking
  wrong result. Refusals name the offending *claim/field*, never its *value* (no
  existence oracles).
- **Three-lens review:** each pillar is examined through three separate adversarial
  perspectives (in isolated worktrees) before it lands — the review catches disclosure
  and correctness regressions the author's own lens misses.

## Consequences

- More explicit guardrails and refusals; fewer silent degradations.
- Review is **heavier per pillar** — a deliberate cost for a system where wrong-but-quiet
  is the expensive failure.
- Other ADRs **assume** this posture (e.g. ADR-002's fail-closed default, ADR-005's prod
  key fail-fast), so weakening it here would quietly weaken them.

## Alternatives considered

- **Best-effort / silent degradation** (return something, log a warning, move on) —
  rejected: in a financial + PII engine it masks exactly the correctness and disclosure
  bugs that matter most, and it makes those bugs trusted.
