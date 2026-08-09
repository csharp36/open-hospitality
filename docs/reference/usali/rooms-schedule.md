# Rooms Schedule (Schedule 1) — Revenue Line Items

## Core segmentation
- **Transient Room Revenue**
- **Group Room Revenue**
- **Contract Room Revenue**

## Other Rooms Revenue (distinct from the three above)
- **No-Show Revenue** — individual/transient guaranteed reservations. Stays in Rooms.
- Day-use rooms, **early departure fees**, **late check-out fees**, rollaway/crib rentals, room surcharges.
- **Executive Lounge Revenue** (12th ed.) — under Other Rooms Revenue if significant.

## Exceptions & cross-schedule routing
- **Group cancellation / attrition fees** → **Miscellaneous Income (Sch 4)**, NOT Rooms.
- **Resort / facility fees** → **Misc Income (Sch 4)** — excluded from Rooms and from ADR (see `misc-income.md`).
- **Early check-in fees** — INFERRED to follow the Other Rooms Revenue pattern; *not directly confirmed.*

## Mapping notes for our source data
- Autoclerk `Room → No Show Charge` and `Room → Cancellation Charge` → Sch 1 Other Rooms Revenue (individual). HIGH.
- Autoclerk `Room → Early Check In` / `Late Check Out` → Sch 1 Other Rooms Revenue (early-checkin inferred). MEDIUM.
- Opera `1010 No Show`, `1015 Day Use` → Sch 1 Other Rooms Revenue. HIGH.
