# OH-27 — SOS cutover decision note

Status: **ACCEPTED (2026-09-07).** The decision D-OH27.9 deferred:
whether, and in what shape, the Summary Operating Statement stops reading
`usali_financial_fact` and starts reading the journal. The gate D-OH27.9 set
— zero parity diff on real data — has now been run and holds (§3). Nothing
is re-pointed until this note is agreed; the design doc's §11 requires the
note to state two specific limits of that gate, and §2 does.

## 1. The decision (PROPOSED)

Re-point the SOS to a journal-derived statement, classified by the chart
(§5's shape B), scoped to `source_type = 'pms_daily'` entries only. The
statement's shape, totals, and error behavior are unchanged for operators;
what changes is which table is the source of truth. The fact tables remain
the staging/mapping layer and the coverage report's subject — they stop
being what the statement renders from.

Why now and not earlier: the journal carries labor (`payroll_accrual`)
that today's SOS deliberately does not show. A statement that silently
gained expense lines the day the journal shipped is the drift ADR-011's
D8.3 forbids; the explicit `source_type` scope plus the parity gate is
what makes the swap invisible instead of silent.

## 2. What the parity gate does NOT cover (per design §11)

Two limits, stated here because the gate's green must not be read as more
than it is:

1. **Parity proves the journal ≡ the *mapped* facts only.** A fact row
   with NULL `gl_account_code` posts no journal line and enters neither
   side of `sos_journal_parity`'s comparison — it sits on neither side, so
   no volume of green parity says anything about mapping coverage. Mapping
   coverage is a separate question and the coverage report owns it
   (`usali coverage`: staged-codes-mapped and GL-mapping counts per
   source). Reading the two together is the operator's job at cutover
   time; the gate alone cannot do it.
2. **The clearing lookup is a convention, restated.**
   `sos_journal_parity` finds the excluded balancing account by
   `system_role == "guest_ledger_clearing"` with no `is_active` filter,
   relying on the per-org unique partial index
   (`uq_gl_account_org_role`: UNIQUE `(org_id, system_role)` WHERE
   `system_role IS NOT NULL`) plus session org-scoping to make that
   scalar lookup well-defined. A deactivated role-bearing account is
   still found — deliberately, since historical entries reference it; the
   thing an org may not do is delete a role-bearing account (D-OH27.2's
   rule, enforced at the API). Any future reader adding an `is_active`
   filter here would silently un-exclude the clearing account and break
   parity from the tool side.

## 3. The gate on real data (2026-09-07)

Run locally against a real choiceADVANTAGE (SkyTouch) Standard Audit Pack
from `~/Desktop/Sample Hotel` — real property financials, which never
enter this repo; property identity and figures live only in the local
evidence file (`scratchpad/parity/EVIDENCE.md` of the session that ran
it).

Method: scratch database migrated to head; schedules, properties, and all
three PMS dictionaries seeded from the repo; the real property registered
through a local-only registry file; the pack's Hotel Journal Summary and
Hotel Statistics pages split out and run through the standard
`usali process` pipeline; `gl-seed-chart`; `gl-post` over the pack's
business date; `reporting.sos_journal_parity` over the same range.

Result: **zero diffs.** Every staged journal code mapped (no NULL
`gl_account_code` rows), the posted entry balanced to the cent, and the
journal's per-account nets equaled the facts' exactly, clearing account
excluded.

Honest limits of that run, which the CI gate does not share:

- One property, one business date, six revenue-side journal rows. The CI
  gate (`tests/test_gl_parity.py`) covers the multi-property seeded
  spread and a mutate-and-repost reversal chain; the real run adds
  realness, not breadth.
- The §2.1 caveat was untestable on this data: the unmapped population
  was empty, so "NULL rows sit on neither side" remains a structural
  claim pinned by unit tests, not something real data exercised.
- Every SKYTOUCH dictionary entry is LOW confidence / needs-review in the
  coverage report. Parity compares the journal against the facts those
  mappings produced — it cannot detect a mapping that is consistently
  wrong on both sides. That is the dictionary review's job, not this
  gate's.
- The statistics page staged cleanly (158 rows) but statistics facts
  carry no GL account and are outside parity by construction — consistent
  with §2.1.

## 4. The gate's actual grain, vs D-OH27.9's prose

D-OH27.9 sketched a diff "per (property, period, USALI line)". What
shipped diffs per **(property, range, GL account)** — finer on the
account axis, but classification-blind: per-account equality implies
per-USALI-line equality only if a fact's USALI classification is a
function of its GL account. Checked on 2026-09-07: across all three
dictionaries (opera, skytouch, autoclerk) every GL account carries
exactly one `(usali_schedule_id, usali_major_category)` pair, so the
implication holds today. The cutover must pin it: a test asserting no
two dictionary entries share a `gl_account` with different USALI
classification (at the full grain the SOS renders, line labels included).
If that invariant ever breaks, per-account parity stops guaranteeing the
statement, and the test is what says so before an operator does.

## 5. Cutover shape (PROPOSED: B)

- **A — classify through the fact link.** Journal lines join back to
  their facts (`journal_line.fact_id`) for USALI classification. Rejected:
  the statement would still depend on the fact rows for its structure, so
  nothing real has been cut over — an out-of-band fact edit still moves
  the statement, which is the exact class of drift the journal exists to
  end.
- **B — classify by the chart.** `gl_account` already carries
  `usali_schedule_id` / `usali_major_category` for income accounts; the
  statement groups journal nets by account and buckets them exactly as
  today (operated/misc by schedule; taxes, settlements, other by major
  category for unscheduled accounts). Two things B requires:
  1. The template's settlement and tax accounts (`1000`, `1100`, `1200`
     cash/card/AR clearing; `2100` tax payable) currently carry NULL
     USALI fields; the dictionaries classify those same accounts as
     "Settlements" / "Taxes (Pass-Through)". The template gains those
     major categories, and — since `seed_chart` is insert-only by design
     (the OH-17 lesson) — existing orgs need an explicit, idempotent
     backfill that fills **only NULL** USALI columns and never overwrites
     an operator's edit.
  2. §4's dictionary invariant test, in CI beside the parity test.
- **C — rewrite parity to USALI-line grain first.** Rejected as
  unnecessary given B's invariant test, and strictly weaker: account
  grain catches misposts that line grain would net away.

## 6. What does not change

- **Scope**: the journal-derived SOS reads `source_type = 'pms_daily'`
  only. Labor stays out of the SOS until a deliberate decision puts it
  in; the trial balance and balance sheet (already journal-read) are
  where the full picture lives.
- The QBO push, `/qbo/preview`, and the CLI dry-run: untouched. The
  preview/dry-run fact-build residual (design §11) neither improves nor
  worsens.
- `NoFactsError` semantics: a property/date range with no posted entries
  must fail the same loud way an empty fact range does today — the SPA
  and CLI already render that refusal.
- The parity tool and `tests/test_gl_parity.py` stay: the gate becomes a
  regression tripwire instead of a precondition.

## 7. Order of operations at flip time

1. B's template change + backfill + invariant test merge first, SOS still
   fact-read (pure additive).
2. The journal-derived statement lands beside the fact-read one;
   `sos_journal_parity` plus a statement-level comparison test hold both
   in CI.
3. Against the production database, run parity (a small
   `usali gl-parity` CLI wrapper) over every property's posted history;
   read it beside the coverage report per §2.1. Zero diffs is the flip
   condition D-OH27.9 set.
4. Re-point the two call sites (`cli.report`, `portal_api`'s SOS
   endpoint) in one commit, revertible by itself.
