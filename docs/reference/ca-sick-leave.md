# California paid sick leave — research findings

**Researched:** 2026-07-20 (user-supplied figures compared against current
sources the same day). **Status:** grounding for Pillar E4's accrual engine —
this document is what unblocked the E4 gate for Pillar E4.
**Not legal advice.** Confirm with counsel before relying on any of this for
payroll.

**Re-check by: 2027-01-01.** The core figures were last amended by SB 616
(effective 2024-01-01) and the legislature has amended this statute repeatedly;
the 2026 session changed qualifying reasons but not the math. Anything encoded
from this page carries this date, the `rules_for()` pattern.

Confidence key: 🟢 confirmed against primary source · 🟡 secondary sources only.

---

## 1. The three numbers (the E4 gate)

All three of the figures proposed for E4 match the current statute. 🟢
Primary sources: Cal. Labor Code §246 (as amended by SB 616, eff. 2024-01-01);
DIR "California Paid Sick Leave: Frequently Asked Questions" (page last
updated 2025-12-31 at time of research).

| Item | Figure | Statute language |
|---|---|---|
| Accrual rate | **1 hour per 30 hours worked** | "at least one hour of paid sick leave for each 30 hours of work" |
| Annual usage cap (employer MAY impose) | **40 hours / 5 days** | limit "use of accrued paid sick days to 40 hours or five days in each year of employment, calendar year, or 12-month period" |
| Accrual/carryover cap (employer MAY impose) | **80 hours / 10 days** | "no obligation … to allow an employee's total accrual of paid sick leave to exceed 80 hours or 10 days" |

Pre-2024 the caps were 24/48 (usage/accrual). **The incumbent's `CA Sick Time
Policy` (balance rendered `20:19`) may well be running the OLD ruleset** —
expect a discrepancy when balances are compared at migration, and do not treat
the incumbent's numbers as a correctness oracle.

## 2. "Or five days" means WHICHEVER IS MORE 🟢

The DIR reads "40 hours or five days" as the **greater** of the two for the
employee's actual schedule, and the same for "80 hours or ten days":

> "an employer must allow an employee to use at least five days or 40 hours,
> whichever is more"

DIR's own examples: a 10-hour/day employee's usage floor is **50 hours**
(5 × 10); a 6-hour/day employee who takes five days (30 hours) still has 10
more hours available to reach the 40-hour minimum. A flat 40/80-hour engine
UNDER-CREDITS anyone whose regular day exceeds 8 hours — this is load-bearing
for hotel staff with long shifts, and it is why E4's caps are computed per
employee, not hard-coded.

## 3. Two compliant structures 🟢

- **Accrual method:** 1 h / 30 h worked, unused hours carry over year to year,
  total accrual cappable at 80 h/10 d (greater-of). Alternative accrual
  schedules are lawful only if the employee has ≥24 h by the 120th calendar
  day of employment and ≥40 h by the 200th.
- **Frontload method:** the full annual amount (≥40 h/5 d) granted up front at
  the start of each year; **no carryover required**.

E4 V1 encodes the **accrual** method — it matches the incumbent's observed
balance behaviour and the design's "accrual runs off approved timecard hours".

## 4. Rate of pay when sick hours are used 🟢

For non-exempt employees, EITHER (employer's choice, applied consistently):

1. the regular non-overtime rate for the workweek in which leave is used, or
2. total non-overtime compensation over the prior 90 days ÷ non-overtime hours
   worked in that period.

For exempt staff: the same method as other paid leave. E4's decision is
recorded in its plan (option 1, resolved through `usali.rates` on the day
taken — the same dated resolution wages use).

### 4a. The G3 payment-path check (re-verified 2026-07-23) 🟢

Pillar G3 makes the provider SUBMISSION pay sick hours, so the shortcut the
SOS has used since E4 — price each sick day at the dated `regular` rate via
`rate_on` — was re-verified against §246(l) before it was allowed to move
money. Statute text confirmed verbatim at leginfo (2026-07-23):

> §246(l)(1): "…calculated in the same manner as the regular rate of pay
> for the workweek in which the employee uses paid sick time…"
> §246(l)(2): "…by dividing the employee's total wages, not including
> overtime premium pay, by the employee's total hours worked in the full
> pay periods of the prior 90 days…"
> §246(l)(3): exempt staff — "…the same manner as the employer calculates
> wages for other forms of paid leave time."

**Why base-hourly is lawful HERE:** the workweek "regular rate of pay"
equals the base hourly rate exactly when the employee has ONE hourly rate
and no other includable compensation that workweek (no commissions, no
nondiscretionary bonuses, no piece rate, no second rate). This system
cannot hold any of those: the E2 rate model stores one dated `regular`
rate per placement, stored premium types make the pay run REFUSE
(`_stored_premium_types`), and a period whose worked days resolve to more
than one distinct rate REFUSES (`_distinct_rates`). So on any run that
actually submits, base-hourly IS the §246(l)(1) figure — and the mixed-rate
refusal is not just a port limitation, it is exactly where a statutory
blended rate would otherwise be required. If commissions/bonuses/multi-rate
submission ever enter the system, this equivalence BREAKS and this section
must be revisited before sick pay ships through that path.

**Payment timing, §246(n)** 🟢: "no later than the payday for the next
regular payroll period after the sick leave was taken." Riding the
period's own pay run satisfies this. Consequence encoded in G3: a usage
entry recorded AFTER its period's run was submitted can never be paid by
that run — it becomes a NAMED preflight blocker on the next run rather
than silently unpaid forever.

*Guard mechanics, updated H6 (2026-07-29):* "recorded after" is no longer
decided by comparing timestamps. Every submitted run stores the sick
hours it paid per employee (`PayRunLine.sick_hours`), and the guard
compares the ledger's CURRENT derivation against that stored figure:
derived > stored → the named blocker above (hours the run provably did
not pay, however and whenever they were recorded — including during the
submission itself); derived < stored → paid-then-voided, a server-side
log; equal → paid, silence. The statute question ("was this sick leave
paid by the payday it was owed?") is answered from what was actually
paid, not from write ordering.

**Re-check with the rest of this document (2027-01-01).**

### 4b. The §246(i) balance display (G5, verified 2026-07-23) 🟢

Verbatim, leginfo:

> §246(i): "An employer shall provide an employee with written notice that
> sets forth the amount of paid sick leave available, or paid time off
> leave an employer provides in lieu of sick leave, for use on either the
> employee's itemized wage statement described in Section 226 or in a
> separate writing provided on the designated pay date with the employee's
> payment of wages."

The notice rides "the designated pay date with the employee's payment of
wages", so the figure G5 sends is the ledger fold **as of the check
date** — `balance_on(employee, check_date)` on every submitted entry,
zero included. Folding at period end would understate a balance that
accrued between period end and the pay date.

Carrier: the provider's pay stub, via `PayRunEntry.sick_balance_hours`.
Capability-gated (`supports_sick_balance_display`): whether real
Gusto/ADP render an externally-tracked balance is unverifiable against
invented mock shapes, so both real adapters declare False — assemble then
sends None ("do not display") and a figure smuggled past the gate refuses
loudly in the adapter. Until the go-live sandbox verifies enablement,
§246(i) compliance for real-provider runs still rests on the provider's
OWN balance display (or a separate writing) — the backlog's go-live item
carries that question.

**Re-check with the rest of this document (2027-01-01).**

## 5. Accrual basis details 🟢/🟡

- "Hours worked" includes overtime hours worked (the statute does not exclude
  them from accrual). 🟢
- **Exempt** employees are deemed to work 40 h/week for accrual purposes
  (unless their normal week is fewer). 🟢 CORRECTED 2026-07-20 by the E4
  adversarial review: the deeming (including the unless-fewer clause) is in
  the TEXT of §246, not merely DIR guidance — and it is not conditioned on
  time capture, which is why the engine deems by weeks EMPLOYED. The
  unless-fewer clause is deliberately NOT encoded (deeming a part-time
  exempt week at 40 over-accrues, which is employee-favorable and lawful);
  recorded in `sick_leave_rules.py`.
- The engine's D (day length for the greater-of caps) is derived as the
  average hours per worked day over the statute's own 90-day lookback,
  floored at 8 when data exists and treated as UNKNOWN (caps skipped,
  employee-favorable) when none does. The DIR's fixed-day examples do not
  prescribe a derivation for variable schedules; this one is recorded here
  as the project's reading.
- The 2026 amendments expand qualifying **reasons** (crime-victim proceedings,
  jury duty/subpoena) — usage-policy scope, not accrual math. 🟡

## 6. Choices the statute leaves to the employer (E4 must pick and record)

- Which 12-month window the usage cap runs over ("year of employment, calendar
  year, or 12-month period").
- Whether to impose the usage and accrual caps at all (they are ceilings on
  limitation, not mandates).
- Accrual vs frontload.

## Sources

- DIR, *California Paid Sick Leave: Frequently Asked Questions* —
  https://www.dir.ca.gov/dlse/paid_sick_leave.htm (last updated 2025-12-31)
- Cal. Labor Code §246 (2025 code text) —
  https://law.justia.com/codes/california/code-lab/division-2/part-1/chapter-1/article-1-5/section-246/
- Cal. Labor Code §246 (FindLaw) —
  https://codes.findlaw.com/ca/labor-code/lab-sect-246/
- HR Ledger, *California's 2026 Paid Sick Leave Law* (confirms no 2026 change
  to the core math) —
  https://www.hrledger.com/california-2026-paid-sick-leave-law-what-you-need-to-know/
