# SkyTouch statistics: DAY and MTD periods never promote (design)

**Date:** 2026-09-20. **Scope:** one defect, one file, one test module.
Standalone PR; nothing from OH-22 rides with it.

## The defect

`adaptors/skytouch_hotel_statistics.py` labels its five value columns
`ACTUAL / PTD / LY_PTD / YTD / LY_YTD` (`_PERIODS`, line 30) and sets
`is_prior_year` on the two `LY_*` columns. `stats_promote._CANONICAL_PERIODS`
knows only `DAY / Today / MONTH / MTD / YEAR / YTD`. So at promotion every
SkyTouch stage row except the plain `YTD` column is counted as `ignored`, and
no `usali_statistic_fact` row with `period = 'DAY'` or `'MTD'` exists for a
SkyTouch property.

Measured on `tests/fixtures/skytouch_hotel_statistics_words.json` (the
committed words of the real Standard Audit Pack section) on main:

| | rows | promoted | ignored |
|---|---|---|---|
| staged by the adapter | 25 (5 metrics x 5 periods, all five metrics curated in `mapping/statistics.yaml`) | | |
| `promote_statistics(source="SKYTOUCH")` today | | 5 (the `YTD` column) | 20 |

Who reads `period == "DAY"`: `performance.py` (occupancy, ADR, RevPAR, `rooms_sold`
from `ROOMS_OCCUPIED`), `night_audit.py`, `schedule_api.py`, `reporting.py`. Every
one of them sees an empty set for a SkyTouch property. The recalibration
that registered the adapter (PR #79, 2026-08-23) verified parsing against the
real pack but never pinned a promoted fact, which is why this survived: the
suite's promote pins are Opera and AutoClerk only.

## Decision

**D1. Fix the map, not the adapter.** Add four rows to
`stats_promote._CANONICAL_PERIODS`:

| raw label (adapter) | canonical period | `is_prior_year` |
|---|---|---|
| `ACTUAL` | `DAY` | as staged (False) |
| `PTD` | `MTD` | as staged (False) |
| `LY_PTD` | `MTD` | as staged (True) |
| `LY_YTD` | `YTD` | as staged (True) |

`is_prior_year` is copied from the stage row, which the adapter already sets,
so `LY_PTD` becomes `(MTD, prior)` exactly the way Opera's `YEAR` with
`prior=True` becomes `(YTD, prior)` in `test_promotes_curated_metrics_and_canonical_periods`.

Rejected: making the adapter emit canonical labels (the shape HotelKey uses on
the OH-22 branch). The stage table is the as-parsed record and already holds
`PTD`-labelled rows for every SkyTouch file ingested so far; those rows are
`ignored`, not `skipped`, so a promote re-run picks them up under D1 with no
data migration. Changing the adapter would strand them and would also churn
the fixture-pinned adapter tests for no gain. Canonicalization already lives
in the promote map for Opera and AutoClerk; SkyTouch joins them.

**D2. Pin the cross-module invariant by name.** The map and the adapter are
in different modules and can drift again. A test that iterates
`skytouch_hotel_statistics._PERIODS` and asserts every label is a key of
`_CANONICAL_PERIODS` fails the moment either side changes alone. The
comment in `stats_promote.py` names that test rather than claiming the
sets agree (per the comment-claims rule in
`docs/design/2026-08-31-comment-claims-invariant-design.md`).

**D3. No backfill in this PR.** Already-ingested SkyTouch files gain DAY/MTD
facts only when promotion runs again for that source and date. There is no
production SkyTouch tenant; the demo re-ingests from the seed. If a backfill
is ever needed it is a one-line re-run of `promote_statistics` per date, not
a migration.

## Out of scope

- `segment_promote._CANONICAL_PERIODS` (`DAY / MONTH / YEAR`): Opera market
  statistics only; SkyTouch has no segment report.
- The HotelKey adapter on #136 emits canonical labels and is unaffected.
- `test_k6b_synthetic_year.py` expects `Today` for AutoClerk and `DAY` for
  Opera at the stage level; it does not assert SkyTouch periods and is
  untouched.

## Acceptance

1. `test_stats_promote.py::test_skytouch_day_and_mtd_facts_promote` stages the
   fixture through the real adapter, promotes with `source="SKYTOUCH"`, and
   finds `ROOMS_OCCUPIED DAY = 62`, `ADR MTD = 90.10`, `ADR MTD prior = 92.25`,
   `ROOM_REVENUE YTD prior = 435800.00`, with `promoted == 25` and
   `ignored == 0`. It must fail on main with `promoted == 5`.
2. `test_stats_promote.py::test_every_skytouch_period_label_is_canonical`
   (D2) fails on main with `{'ACTUAL', 'PTD', 'LY_PTD', 'LY_YTD'}` missing.
3. Full pytest, ruff and `mypy --strict src` green.
