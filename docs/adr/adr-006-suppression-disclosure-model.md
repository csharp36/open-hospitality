# ADR-006: Suppression &amp; disclosure model

- **Status:** Accepted
- **Date:** 2026-08-09
- **Deciders:** Open Hospitality maintainers

## Context

Labor-cost reporting aggregates money by department and day. But a thin department can
leak an **individual's pay rate**: if a day's cost belongs to one priced person,
`cost ÷ hours` recovers that person's rate. The engine must publish useful cost figures
without becoming a rate-disclosure oracle — and it must do so uniformly, since a single
inconsistent path defeats the whole model.

## Decision

We will apply one suppression rule, complementary and direction-aware.

- **Threshold:** a department's money column is disclosed only when **every
  cost-carrying day has ≥ 2 distinct *priced* employees.** "Priced," not merely present:
  an `est_cost == 0` employee (FLSA-exempt salary, or no rate on file) must **not** let a
  solo-priced department escape suppression.
- **Complementary:** suppressed values are **excluded from totals**, so a reader cannot
  recover a hidden value by subtracting visible ones from a grand total.
- **One implementation** of the rule — not a report copy and an API copy that can drift.
- **Direction-aware disclosure:** an inbound, read-only feed carries **zero** disclosure
  surface; an **outbound** feed (notification, webhook) can reveal a suppressed value and
  is **un-auditable** (you cannot audit who read a message), so outbound feeds must obey
  the same suppression as the API and are the last thing built.
- Every money read is treated as a **per-person disclosure** that writes an audit event.

## Consequences

- Some legitimately thin departments show a suppressed column — **safety over
  completeness**, by design.
- Outbound integrations carry extra review weight and are deliberately sequenced after
  inbound ones.
- The audit trail on money reads grows, which is the point: disclosures are accountable.

## Alternatives considered

- **Redaction without excluding from totals** — rejected: subtraction against the total
  recovers the redacted value.
- **Per-report tunable k-anonymity thresholds** — rejected: multiple thresholds invite
  drift and an inconsistent guarantee; one rule, uniformly enforced, is auditable.
