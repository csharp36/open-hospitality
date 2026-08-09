# Source: Autoclerk 5.x

Property in sample data: **SureStay Plus by BW, San Jose Central City** (business date 07/07/2026).

## Files provided (`docs/reference/sources/autoclerk/`)
| File | Contents |
|------|----------|
| `AutoClerk_Transaction_and_Rate_Code_Extract.xlsx` | Multi-sheet extract: README, Transaction Codes, Rate Codes, Manager Summary, Combined list, raw report dumps. |
| `Autoclerk - Transaction Summary.csv` | **Daily financials** — category → name with TODAY / MTD / YTD amounts. |
| `Autoclerk - Manager Flash.csv` | Manager's report: income, payments, ledgers, occupancy, ADR/RevPAR, forecast. |
| `Autoclerk - Revenue by rate plan.csv` | 21 rate plan codes with room nights, ADR, revenue. |

## Critical limitation
> From the extract's own README: *"these reports show transaction/rate names but do not
> expose AutoClerk internal numeric seed/code IDs."*

**There are no real transaction codes.** Available data is ~32 report-derived
category/name pairs plus 21 rate-plan codes.

## Transaction categories observed
Room, Cash, Tax, Credit Cards, Accounts, Misc, Laundry, CLC Direct Bill, Airbnb Direct
Bill, Canary, Parking, HIE Market Sell. Names include: Room Rent, Early Check In, Late
Check Out, Cancellation Charge, No Show Charge, Occupancy Tax, County Tax, Tourism Fee,
Pet Fee, Parking Fees, Water, Soda, Room Upgrade 1/2, etc.

## Design consequence
- `pms_trx_code` must be a **synthetic composite key**, e.g. `ROOM|ROOM_RENT`,
  `TAX|OCCUPANCY_TAX`, until a supervisor config export with real IDs is available.
- Autoclerk mappings carry **lower confidence** than Opera by construction.

## Upside vs Opera
Autoclerk **does** provide real **daily financials** (Transaction Summary), so it is the
first source that can validate the stage → transform → unified pipeline against real
numbers — even while its catalog is weak.

## Daily deliverables (see `ingestion-contract.md`)
The real daily feed arrives as **PDF** (emailed to Inn-flow). Three Autoclerk reports:
- **Transaction Summary** — financial feed (category/name + TODAY/MTD/YTD). No codes → synthetic keys.
- **Manager Report** — statistics/KPIs + ledgers + occupancy + forecast.
- **Revenue by Rate Plan** — room nights & revenue by rate code.

## Open item
A **supervisor/config export with internal code IDs** would replace synthetic keys and
raise mapping confidence — still worth obtaining, but not blocking (the Transaction
Summary PDF is a sufficient financial feed for V1).
