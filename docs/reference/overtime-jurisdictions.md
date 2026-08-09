# Overtime jurisdictions — research findings

**Researched:** 2026-07-18. **Status:** grounding for `src/usali/overtime_rules.py`.
**Not legal advice.** Confirm with counsel before relying on any of this for payroll.

Confidence key: 🟢 confirmed against primary source · 🟡 corroborated by multiple
secondary sources only · 🔴 commonly-repeated claim found to be **false**.

---

## 1. Multi-location aggregation (the E1 question)

**Question:** one LLC, two hotels, an employee works 6h at one and 5h at the
other on the same day. Is that 11 hours with 3 hours of daily overtime?

**Answer: yes — hours aggregate.** 🟢

The mechanism is definitional, not a special rule:

- **Cal. Labor Code §500** — *"'Workday' and 'day' mean any consecutive 24-hour
  period commencing at the same time each calendar day."* Note what it does not
  say: there is no "at a given work site" qualifier. The workday is temporal and
  attaches to the employee-employer relationship, not to a building.
- **Cal. Labor Code §510(a)** — over 8 hours in *one workday* at 1.5×, over 12 at
  2×, plus the seventh-consecutive-day premium.
- **IWC Wage Order 5 (public housekeeping — hotels), 8 CCR §11050 §2** —
  "employer" is any person per Labor Code §18 who "exercises control over the
  wages, hours, or working conditions of any person"; "hours worked" is "the time
  during which an employee is subject to the control of an employer." Both are
  employer-scoped, not site-scoped.

A single LLC is one "person" under §18, so all hours worked for it in a workday
fall in the same workday. 6 + 5 = 11, and 3 hours are daily overtime.

**Federal floor agrees independently.** 29 CFR §778.103: *"the employer must
total all the hours worked by him for [the employer] in that workweek (even
though two or more unrelated job assignments may have been performed)."* FLSA is
weekly-only and has no daily or double-time concept, so California's more
protective daily rules control (FLSA §218(a) savings clause).

**No contrary authority found.** There is no statutory, regulatory, or case
authority permitting two locations of a single legal employer to be treated as
separate overtime pools. The only real escape valves are (a) genuinely separate
unaffiliated employers, (b) a validly adopted alternative workweek schedule
under Labor Code §§511/514/554 — which changes *when* the 8-hour trigger fires,
not whether hours combine — and (c) a true independent-contractor relationship.

### Honest gaps in this finding

- **No single on-point DLSE opinion letter** captioned on multi-establishment
  aggregation was located. The conclusion rests on the statutory definitions
  above, which is solid but is not the same as a agency pronouncement directly
  on the fact pattern.
- **DLSE Opinion Letter 1991.11.01-4** ("Calculation for employee working at two
  different hourly rates") is the closest on-point authority and supports both
  aggregation and a weighted-average regular rate. It is a scanned 1990s PDF and
  the summary of its contents was **not verbatim-verified**. Do not cite it in a
  filing without reading the original.

### ⚠️ The nuance that matters most for El Sendero

Everything above assumes **one legal employer**. Hotel ownership commonly uses a
separate LLC per property. If SJCES and 58033 are actually held by *different*
entities that share staff, aggregation is no longer automatic — it becomes a
**joint-employer** analysis:

- **Federal:** DOL Opinion Letter FLSA-2025-05 found a hotel restaurant and an
  adjoining members-only club — nominally separate entities — were *horizontal
  joint employers* because of common ownership, overlapping management, shared
  scheduling, identical pay rates, and interchangeable assignment. Hours had to
  be aggregated **despite separate incorporation, separate timekeeping, and
  separate payroll**. That fact pattern is close to a two-hotel shared-staff
  operation.
- **California:** *Martinez v. Combs*, 49 Cal. 4th 35 (2010) — to "employ" means
  to exercise control over wages/hours/working conditions, to suffer or permit to
  work, or to engage. Control by a common owner can create joint employment even
  without hiring/firing authority. 🟡 (holding widely reported; slip opinion not
  independently pulled)
- **Regulatory status is unsettled:** 29 CFR Part 791's 2020 rule was largely
  vacated in 2020 and the vertical test rescinded in 2021. A new DOL NPRM
  (published ~April 2026, comments closed June 2026) is **not yet final**. Courts
  apply pre-2020 economic-realities case law in the interim. **Do not hardcode a
  federal joint-employer test without a re-check date.**

**Practical effect on our design:** either way, aggregation is the correct
behaviour here — separate entities that share staff this closely would very
likely be joint employers and aggregate anyway. The design is safe. But the
*reason* differs, and it is worth confirming the actual entity structure.

---

## 2. Fifty-state variation

### Daily overtime

| Jurisdiction | Daily threshold | Multiplier | Confidence |
|---|---|---|---|
| California | >8 (to 12) | 1.5× | 🟢 Lab. Code §510; 8 CCR §11050 |
| Alaska | >8 | 1.5×, **no double-time tier** | 🟢 AS 23.10.060. Employers with <4 employees exempt. No-pyramiding against weekly 40. |
| Nevada | >8, **only if paid under 1.5× state minimum wage** | 1.5× | 🟢 NRS 608.018. Threshold moves with minimum wage. Inapplicable if the employee agreed in writing to 4×10. |
| Colorado | Greater of: >40/week, >12/workday, or **>12 consecutive hours** regardless of workday boundary | 1.5× | 🟢 7 CCR 1103-1 Rule 4.1.1–4.1.3 (COMPS Order; number revises ~annually) |
| Oregon | >10/day — **manufacturing/mills/canneries only**, not general industry | 1.5× | 🟢 ORS 652.020, 653.265. Pay the greater of daily or weekly. |
| Puerto Rico | >8/calendar day | 1.5×, **or 2× for pre-2017 hires** | 🟡 Act 379 of 1948 as amended by Act 4 of 2017; primary bilingual text not fetched |

### Double time

Only **California** 🟢 and **Puerto Rico** 🟡.

🔴 **"Alaska has double time" is FALSE.** It is widely repeated in secondary
sources, and it is contradicted by both AS 23.10.060 and Alaska DOL's own
summary, neither of which contains any 2× tier. Worth knowing precisely because
it is the kind of claim that gets copied into a rules engine unchecked.

### Seventh consecutive day

| Jurisdiction | Rule | Confidence |
|---|---|---|
| California | 1.5× first 8 hours, 2× beyond 8 | 🟢 Lab. Code §510(a) |
| Kentucky | 1.5× for **all** hours on the 7th day, but **only if** the employee worked over 40 that week; creditable against weekly OT rather than stacked | 🟢 KRS 337.050 — materially narrower than California |

No other state found with a seventh-day premium. 🟡 (secondary-source consensus,
not a 50-state primary audit).

### Higher-than-40 weekly thresholds

Kansas (46/wk) and Minnesota (48/wk) set higher *state* thresholds — but the
FLSA's 40-hour floor still governs any FLSA-covered employment, which is nearly
all commercial employers. These numbers matter only for employment outside FLSA
coverage. 🟡

### Everything else

The remaining ~44 states and DC have no daily-overtime and no double-time
concept; they either have no state overtime statute or restate the FLSA. 🟡
**Model FLSA as the default and build override records only for the six or seven
deviating jurisdictions** — do not attempt to enumerate 44 identical entries.

---

## 3. What our vocabulary cannot yet express

`OvertimeRules` models daily/weekly thresholds, double-time, and a
seventh-day premium. Per that module's own rule — *do not bend an existing field
to nearly mean the right thing* — these jurisdictions need vocabulary extensions
before they can be encoded:

| Jurisdiction | Missing concept |
|---|---|
| Colorado | Consecutive-hours trigger that ignores workday boundaries, and greater-of-three semantics |
| Nevada | Rule conditional on the employee's wage relative to minimum wage |
| Kentucky | Seventh-day premium conditional on exceeding 40 that week, and creditable rather than additive |
| Oregon | Industry-scoped applicability (manufacturing only) |
| Puerto Rico | Multiplier conditional on hire date |
| Alaska | Employer-size exemption (<4 employees) — an employer attribute, not a rule threshold |

**Alaska is otherwise cleanly expressible** and is the natural next state to
encode if one is needed.

## 4. Staleness risks

These are moving targets. Anything encoded should carry a re-check date:

- **Nevada's threshold** tracks the state minimum wage.
- **Colorado's COMPS Order** is revised roughly annually.
- **Oregon** has active legislative movement on overtime calculation.
- **Federal joint-employer rule** has a pending NPRM not yet final.
