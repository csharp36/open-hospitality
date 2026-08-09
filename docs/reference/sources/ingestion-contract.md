# Ingestion Contract — the daily deliverables

The system replicates **Inn-flow**, which receives these reports **as PDF attachments via
email**. The PDFs in `docs/reference/samples/` are the exact files sent. **PDF is therefore the primary
ingestion format**, not XML/CSV. (The Opera XML catalog and the Autoclerk CSVs we also have
are secondary: the XML seeds the mapping dictionary; the CSVs are convenience exports.)

Business date of all samples: **07/07/2026**.

## Two properties, two PMSs (proves multi-property / multi-brand)
- **Opera** → *Holiday Inn & Suites San Jose* (IHG).
- **Autoclerk** → *SureStay Plus by BW, San Jose Central City*.

## Parallel three-report structure

| Role | Opera report (`internal id`) | Autoclerk report | Purpose |
|------|------------------------------|------------------|---------|
| **Financial** | Trial Balance (`trial_balance`) | Transaction Summary | Transaction codes/names + daily amounts. **Only report with codes → drives USALI mapping + fact table.** |
| **Statistics** *(implemented P4)* | Manager Flash (`manager_flash`) | Manager Report (`manager_report`) | Occupancy, ADR, RevPAR, room counts, arrivals/departures/no-shows, revenue summary. |
| **Segmentation** *(implemented P5)* | Market Code Statistics (`stat_dmy_seg`) | Revenue by Rate Plan | Room nights + revenue by **market segment** (Opera) / **rate code** (Autoclerk) → Transient/Group/Contract split. |

## Financial report detail (the core feed)

### Opera Trial Balance
- Groups: **Revenue**, **Non Revenue**, **Payment** (Opera's own high-level buckets — usable as a cross-check: revenue codes must map to revenue schedules; payments must not hit revenue).
- Per line: `TRX_CODE`, description, **Today** amount. Only codes with activity that day appear (~14 in the sample; full universe is the 478-code catalog).
- Sample codes: `1000` *Accommodation, `5105` Parking, `5106/5107` Gift Shop, `5007` Hotel BID Fee, `7100` TOT 10%, `7101` CCFD 4%, `7102` CA Tourism, `7104` Sales Tax, `9002-9007` payments.
- Also carries ledger movements (Guest/AR/Deposit/Package) and balance checks — not revenue, but useful for reconciliation.

### Autoclerk Transaction Summary
- Category → name rows with TODAY / MTD / YTD. No numeric codes → synthetic `CATEGORY|NAME` key.
- Categories: Room, Cash, Tax, Credit Cards, Accounts, Misc, Laundry, CLC Direct Bill, Airbnb, Canary, Parking, HIE Market Sell.

## Statistics report detail
- **Opera Manager Flash:** DAY / MONTH / YEAR columns for both current (2026) and prior (2025) year. Metrics: % Rooms Occupied, ADR, RevPAR, Total/Room/F&B/Other Revenue, room counts, arrivals/departures, No Show Rooms, in-house persons, etc.
- **Autoclerk Manager Report:** Folio charges/income by category, payments, three ledgers (Guest/Advance Deposit/City) with debits/credits, occupancy, ADR, RevPAR, statistics, city-ledger aging, 14-day forecast.

## Segmentation report detail
- **Opera Market Code Statistics:** by Market Group/Market Code (D Discount, G Corp-Global, J/K Package, L Corp-Negotiated, M Gov/Dipl/Military, N Complimentary, P Corp-Key/BTA, V Industry, W Wholesale/FIT, Y Long-Term, Z Group). Columns: Rooms, Room Revenue, ADR, % Occ, Persons — for DAY/MONTH/YEAR. Grand total ties to Manager Flash room revenue (2,021,427.81 YTD).
- **Autoclerk Revenue by Rate Plan:** 21 rate codes (15C, RACK, GTL, EC6…) with Room Nights, ADR, Room Revenue, Non-Room Revenue, Total.

## Parsing notes
- All are machine-generated reports with **stable templates** → parseable with positional/tabular PDF extraction (e.g. pdfplumber). Each (source, report-type) is its own template parser.
- Amounts use `$ 1,234.56` / `-$ 1,234.56` (Autoclerk) and `1,234.56` / `- 1,234.56` (Opera). Normalizer must handle both sign conventions.
