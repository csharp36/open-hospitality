# OH-27 — SOS cutover (plan)

Status: **APPROVED (2026-09-07).** Executes
[`2026-09-07-oh27-sos-cutover-decision.md`](../design/2026-09-07-oh27-sos-cutover-decision.md)
and its accepted choices (shape B; `pms_daily` scope; account-grain gate
plus the §4 invariant test). If the note is ever amended, this plan changes
first. Subagent-driven, one task per implementer with spec and
quality review, per the working style.

Ordering follows the note's §7: everything through T5 is additive and lands
with the SOS still fact-read; T6 is the flip, one commit, revertible alone.

## T1 — the dictionary invariant test

The §4 bridge: per-account parity guarantees the statement only while a
fact's USALI classification is a function of its GL account. Add a CI test
(beside `tests/test_gl_parity.py`) that loads every mapping dictionary
(opera, skytouch, autoclerk — enumerate by glob so an eighth dictionary
cannot dodge it) and asserts no `gl_account` carries two distinct USALI
classification tuples **at the full grain the SOS renders** (schedule,
major, and the sub/line labels `_grouped_lines` groups by), across files as
well as within one. Holds today (verified 2026-09-07); the test exists to
say so the day it stops.

- Accept: test fails when a copied dictionary entry is given a second
  classification for an existing account (prove by temporary mutation);
  green on the repo as-is.

## T2 — the template's settlement and tax majors

`mapping/gl_accounts_usali.yaml`: `1000`/`1100`/`1200` gain
`usali_major_category: "Settlements"`, `2100` gains
`"Taxes (Pass-Through)"` — the values the dictionaries already use for
those accounts (the template's own header says its USALI fields are copied
from the dictionaries; this completes the copy). Schedules stay NULL:
these are the unscheduled buckets, same as fact-side.

- Accept: seeding a fresh org yields chart rows whose classifications
  reproduce every bucket of today's fact-read SOS; T1's test still green.

## T3 — the NULL-only USALI backfill

`seed_chart` is insert-only by design (the OH-17 re-seed lesson), so
existing orgs never receive T2's values. Add an explicit, idempotent
backfill — `usali gl-seed-chart --fill-usali` (or a sibling command if the
flag reads badly in review) — that updates **only NULL** `usali_*` columns
from the template, matched by `account_code`, and never touches a non-NULL
value or any other column. Report counts (filled / left alone).

- Accept: a chart where an operator renamed an account and hand-set one
  USALI field keeps both after the backfill while its NULL fields fill;
  running twice is a no-op the second time.

## T4 — the journal-derived statement

A new `reporting` function producing the same `SosReport` shape from
journal lines: entries where `source_type = 'pms_daily'` for the property
and range, nets per account (credit-positive, the parity convention),
classified via the chart per shape B — operated/misc by schedule,
taxes/settlements/other by major for unscheduled accounts, clearing
excluded by `system_role`. Empty result raises the same loud refusal the
fact path's `NoFactsError` gives (same class or a sibling the callers
already render). Pure read, `reporting.py` style.

The pinning test is statement-level, not just totals: on the seeded
samples, render the fact-read SOS and the journal-read SOS after a full
`gl-post` backfill and assert the reports are **equal field-for-field**
— the strongest form of the parity claim, at the grain operators see.

- Accept: equality test green on seeded samples and survives the
  reversal-chain scenario from `test_gl_parity.py`; empty range refuses
  loudly; labor entries posted for the same range change nothing.

## T5 — `usali gl-parity`

The note's §7.3 needs the gate runnable against production. A CLI command
wrapping `reporting.sos_journal_parity`: property and date range (or
`--all-properties` walking posted history), prints per-account diffs,
exits nonzero when any exist. No new logic — the tool is the function.

- Accept: zero-diff run exits 0 and says so; a hand-broken journal line in
  a scratch DB produces the diff row and exit 1.

## T6 — the flip

Re-point both call sites — `cli.report` and the portal SOS endpoint — to
T4's function, in one commit containing nothing else. The fact-read
function stays (the coverage report and reconciliation still read facts);
its docstring gains the one-line pointer that the SOS no longer renders
from it. `tests/test_gl_parity.py` and T4's equality test remain in CI as
the tripwire. Update design doc §11 (the cutover residual resolves,
pointing here), `docs/ROADMAP.md`, and `.github/roadmap.yml` if OH-27's
entry is affected.

**Gate before this task runs** (operator step, not a subagent's): T5
against the production database over every property's posted history,
read beside `usali coverage` per the note's §2.1 — parity green AND
coverage read consciously. Recorded in the PR description.

- Accept: SPA `/reports` and `usali report` render identical statements
  before and after on the seeded samples; a fact edited out-of-band no
  longer moves the statement (a test proves the decoupling — the exact
  drift class the journal exists to end).
