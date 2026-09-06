# ADR-011: The general-ledger posting model

- **Status:** Accepted
- **Date:** 2026-09-06
- **Deciders:** Open Hospitality maintainers

## Context

The 2026-09-06 roadmap revision (ROADMAP §1.1, from
`reference/competitive-positioning-2026-09.md`) commits OH to carrying a
full general ledger, built in slices, with OH-27 — the posting core — first
because every later slice (bank reconciliation, AR/AP, multi-entity,
year-end) posts into it.

Much of a posting core already exists in disguise. `qbo_push.build_journal_entry`
builds a balanced, double-entry journal-entry plan per (property, business
date) from `UsaliFinancialFact` rows, against a chart of accounts in
`mapping/qbo_accounts.yaml`, with a balancing line and loud refusals for
unmapped codes — and then hands the result to QuickBooks. `FiscalCalendar`
already defines periods per property. What is missing is a home: a journal
table of our own, a chart of accounts that is data rather than a YAML file,
period close, and the statement reading from the journal.

Becoming the ledger raises the stakes on every choice here: a mapping bug was
a misfiled statistic; a posting bug is a misstatement. The decisions below are
the ones that are expensive to reverse once real books exist.

## Decision

We will build the posting core on five rules.

1. **The chart of accounts is per-org data, seeded from a USALI template.**
   A `gl_account` table, `OrgScoped` like every tenant table, seeded at
   provisioning from a repo-shipped USALI (11th ed.) template — the successor
   to `mapping/qbo_accounts.yaml`. Orgs extend by adding accounts; template
   accounts carry their USALI schedule/line identity so statements stay
   comparable across orgs. Per-org copies, not a shared base with overrides:
   RLS then covers the chart exactly like everything else, and no
   two-layer resolution exists to get wrong. (This deliberately does NOT
   settle the *mapping dictionary* tenancy question — OH-20's open decision —
   which has different forces: dictionaries are large, mostly shared, and
   curated centrally.)

2. **The journal is append-only; corrections are reversals.** Journal
   entries and their lines are never updated or deleted after posting. A
   wrong entry is corrected by posting its reversal and the corrected entry,
   all three visible. Enforcement is in Postgres, not convention — the
   application role is denied UPDATE and DELETE on the journal tables — per
   the ADR-010 posture that a wall lives in the database or it is not a wall.

3. **Facts post through a deterministic, idempotent posting engine.** The
   staged-fact pipeline (detect → parse → stage → promote) stays the
   ingestion product; a posting engine derives journal entries from facts —
   generalizing `build_journal_entry` — and every journal line carries links
   to the facts behind it. Posting is idempotent per (property, business
   date, source), the `QboPushLedger.request_hash` precedent: re-promoted
   facts produce a reversal-and-repost, never a duplicate or an edit. Labor
   facts and, later, bank/AR/AP transactions enter through the same engine
   as new sources, not through side doors.

4. **Journal amounts are exact.** `Decimal` end-to-end in Python, `Numeric`
   in Postgres, and a balance check (debits equal credits, exactly) enforced
   per entry by a database constraint at post time. The existing fact tables'
   `float` mapping is tolerable for analytics; it is not tolerable for a
   ledger, and fact amounts are converted at the posting boundary.

5. **Periods close per property; a closed period refuses postings, loudly.**
   Close is an explicit, audited act per (property, fiscal period) against
   the property's `FiscalCalendar`. Posting into a closed period is refused
   with an explicit error naming the period — never silently redated.
   Reopen exists, is equally audited, and is org_admin-only; late-arriving
   corrections either reopen or post as a current-period adjustment entry
   that names the source period. "Closed" is also the export trigger: the
   QBO push becomes an export *from* the journal, so owners whose CPA wants
   QuickBooks lose nothing.

The trial balance and balance sheet read from the journal; the Summary
Operating Statement is re-pointed at the journal, with drill-through running
statement line → journal lines → facts → staged rows.

## Consequences

- Bank reconciliation (OH-28), AR/AP (OH-29), and the below-GOP work (OH-30)
  all land on one immutable journal instead of each inventing its own store.
- Reversal-based correction makes the books auditable and makes "what changed
  since close" answerable — at the cost of a noisier journal than
  edit-in-place products show. The UI can collapse reversal pairs; the data
  never hides them.
- Per-org charts mean a template improvement does not auto-propagate to
  existing orgs; template updates need an explicit, versioned migration
  story. Accepted: silent chart drift under an org's feet is worse.
- The `Decimal` boundary adds conversion friction against the float-mapped
  fact columns and is deliberate one-way pressure to tighten those types
  over time.
- Two report paths exist during the transition (statement-from-facts and
  statement-from-journal); the cutover must be verified by diffing the two
  on real data before the fact path is retired.

## Alternatives considered

- **Shared global chart with per-org extension rows** — rejected: crosses
  the RLS grain (a half-global, half-scoped read path), and the
  franchise-configurable reality that broke the global mapping dictionary
  (ROADMAP §4.1) argues against repeating the shape for accounts.
- **Editable journal entries with an audit log** — rejected: an audit log
  records the tampering it permits. Append-only plus reversals is the
  accounting convention, and it is enforceable by grants rather than review.
- **Posting directly at ingestion (no separate engine)** — rejected: parsing
  and posting fail differently and retry differently, and bank/AR/AP sources
  will not arrive through the PDF pipeline at all.
- **Floats, matching the fact tables** — rejected: exact balance is the
  defining invariant of a ledger; "balanced to within epsilon" is not a
  ledger.
- **Redating late postings into the open period silently** — rejected by the
  ADR-010 posture: loud refusal over silent convenience, every time.
