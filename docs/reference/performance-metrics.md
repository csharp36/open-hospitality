# Performance metrics — foundations (rooms available & fiscal periods)

Issue #8 establishes the two denominators every performance statistic needs.

## Rooms available

For an inclusive date window `[start, end]` at a property:

    rooms_available = Σ_day∈window ( total_rooms in force that day )
                    − Σ_block ( overlap_nights(block, window) × block.room_count )

- The in-force room count for a day is the `room_inventory` row with the
  greatest `effective_date ≤ day`. Counts are effective-dated and append-only;
  history is never rewritten.
- Each out-of-order block is clamped to the window before counting nights.
- If any day in the window has no in-force inventory row, the computation
  refuses (`InventoryNotConfigured`) rather than assuming a count.

Implemented in `src/usali/inventory.py` (`rooms_available`, `total_rooms_on`).

## Fiscal periods

Each property has one `fiscal_calendar` row. Period keys are `"{fiscal_year}-Pnn"`,
where `fiscal_year` is the calendar year the fiscal year starts in and `nn` is
`01`–`12` (both calendar types have twelve periods).

- **calendar_month:** period *N* is the *N*-th calendar month from
  `fiscal_year_start_month`.
- **4-4-5:** the fiscal year is anchored on the first `week_start_weekday` on or
  after the 1st of the start month; periods are 4/4/5-week blocks per quarter. A
  53-week fiscal year is handled by the final period absorbing the extra week, so
  the calendar always tiles up to the next year's anchor.

Implemented in `src/usali/fiscal.py` (`resolve_period`, `period_containing`,
`periods_in_year`).
