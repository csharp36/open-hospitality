# Competitive positioning and feature priorities — September 2026 (rev 2)

> Generated 2026-09-06 from the published brief (Claude-assisted, with every external claim re-verified against primary sources; see the verification log). Rev 2 adds the seven-integration target and the general-ledger decision. Supersedes `competitive-landscape-2026-09.md`. Companion to `pms-variants.md`, `build-vs-integrate.md` and `sources/ingestion-contract.md`.

# Open Hospitality: where it fits and what to build next

A standalone reference anchored in the public repository at `github.com/csharp36/open-hospitality` (main at `5b01df5`, PR \#117) and its roadmap catalogue, with a re-verified competitive analysis of the hotel back-office market and a stack-ranked feature list derived from it.

Revision 2, 6 September 2026. Supersedes the 3 September brief. Every external claim was re-checked on 6 September; the verification log at the end records what held, what was corrected, and what could not be confirmed. This revision adds two decisions: a target of seven PMS integrations, and a full general ledger on the roadmap.

## Summary

**The layer.** The industry calls what M3, Inn-Flow, Aptech and Otelier sell "hotel accounting software" (Hotel Tech Report's category) or "back-office" software (Inn-Flow and Otelier's own words). Open Hospitality is in that layer. The most precise self-description is *the hotel accounting and labor layer between the PMS and the general ledger*.

**The basics, measured.** Inn-Flow publishes the eighteen PMSs it ingests from and how. OH today has real parity on three of them (Opera, AutoClerk, Choice's audit pack) and none of the other US brand systems. The target is **seven PMS integrations**: HotelKey (Hilton PEP, IHG limited-service, G6, ESA, BWH's AutoClerk Atlas), choiceADVANTAGE, OPERA (files today, OHIP next), Sabre SynXis Property Hub, Marriott FOSSE/Agilysys Stay, Visual Matrix, and Cloudbeds. Together those reach roughly 60% of US properties and about 80% of branded ones; the independent tail comes through aggregators, not an eighth parser.

**The general ledger.** Revision 1 said "not the GL". This revision reverses that: OH will carry a full-featured GL, built in stack-ranked slices, with bank feeds through Plaid or Yodlee rather than statement uploads from v1. The reasoning is that a 2026 build with Claude Code can reach the basics M3 and Inn-Flow took years to assemble, that OH already produces a journal (it just hands it to QuickBooks), and that a five-hotel owner's real pain is the books and the operating data living in different systems. Replicating QuickBooks is not the goal; the basics are, done natively for hotels. Two existing decision records change as a result: the "bank rec: don't build" verdict in `build-vs-integrate.md` and the bank-credential posture in Pillar E5.

**The ranking.** HotelKey stays first. The GL core (double-entry posting, chart of accounts, periods, close) moves into Tier 0 because every later slice, including bank reconciliation, posts into it. Bank connection and reconciliation lead the GL slices. Connectivity to the remaining PMSs, parity features and the modern-stack items are re-sequenced around that spine.

## Glossary

Terms as they are used in this document and in the vendor material it cites.

| Term                                        | Meaning                                                                                                                                                                                                                                                  |
|---------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **PMS**                                     | Property management system. The front-desk system of record: reservations, check-in, folios, room status, the night audit. Oracle OPERA, HotelKey, choiceADVANTAGE, AutoClerk, Cloudbeds, Mews.                                                          |
| **CRS / GRS**                               | Central (or, at IHG, Guest) reservation system. The brand's booking engine and distribution hub; the PMS must talk to it. IHG's GRS runs on the Concerto platform and was built with Amadeus.                                                            |
| **Night audit**                             | The end-of-day close in the PMS that rolls the business date, posts room and tax, and produces the standard report pack (trial balance, manager's report, market segment statistics). Back-office vendors live on this pack.                             |
| **USALI**                                   | Uniform System of Accounts for the Lodging Industry (HFTP, 11th edition). The chart-of-accounts and statement structure hotel owners, lenders and brands expect. Schedule 14/15 are the labor schedules OH computes.                                     |
| **GL**                                      | General ledger: the accounting system of record for money (QuickBooks Online, NetSuite, Sage, M3 Accounting Core). OH pushes journal entries to QuickBooks today and, per this revision, becomes one itself (OH-27).                                     |
| **Hotel accounting / back-office software** | The layer between the PMS and the GL: revenue mapping, income journal, reconciliation, labor, budgets, reporting. Hotel Tech Report's category name is "Hotel Accounting Software"; Inn-Flow and Otelier say "back-office platform".                     |
| **Franchisor-approved PMS**                 | A PMS a brand permits its franchisees to run because it is integrated with the brand's CRS and loyalty. Hilton, Marriott, Choice, Wyndham and Accor mandate one; IHG keeps an approved list (HotelKey, OPERA Cloud, Shiji in China).                     |
| **Chain scale**                             | STR/CoStar's six brand tiers from economy to luxury. "Select-service" or "limited-service" hotels (Holiday Inn Express, Hampton, Comfort) are where OH's labor model and owners' pain both concentrate.                                                  |
| **Datastream / HK API**                     | HotelKey's event feed and REST API. HotelKey's masterlist states every listed partner uses these; credentials are issued per property on request.                                                                                                        |
| **OHIP**                                    | Oracle Hospitality Integration Platform: the REST API for OPERA Cloud. Partners join Oracle PartnerNetwork, list on Oracle Cloud Marketplace, then each hotel approves the connection.                                                                   |
| **Hapi**                                    | An integration hub (14,000+ connected hotels) that normalizes PMS data, including folios and charges, from OPERA, HotelKey, SynXis Property Hub, Stayntouch and others. Partners pay per connected hotel; the hotel or chain must authorize the link.    |
| **Omniboost**                               | Accounting-specific middleware that turns the night-audit revenue journal from Cloudbeds, Mews, Apaleo, OPERA and choiceADVANTAGE into GL entries. The nearest thing to a plug-in "80% of cloud PMSs" path.                                              |
| **Approved Vendor**                         | IHG's designation for third-party products it has vetted for franchisees (UniFocus and Deputy for workforce management, Canary for digital tipping). Run by IHG Owner Solutions with procurement. Not a marketplace, not required to sell to IHG owners. |
| **RLS**                                     | Row-level security. OH's tenant wall is enforced in Postgres, fail-closed, rather than in application code.                                                                                                                                              |

## Where OH is today, from the public repository

Read from `.github/roadmap.yml` and `docs/ROADMAP.md` at `5b01df5`. The local checkout and GitHub main are identical, so this is the public state.

| Area                   | Shipped                                                                                                                                                                                                                                                                  | In progress / planned                                                                                           | Considering                                              |
|------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|----------------------------------------------------------|
| **Ingestion**          | Detect → parse → stage → promote; adapters for Opera (trial balance, manager flash, market stats), AutoClerk (transaction summary, manager report, rate plan), and the Choice audit pack via `process_pack` (hotel journal, hotel statistics). Anonymous `/try` preview. | OH-22 direct PMS integrations (planned); HotelKey adapter scoped in `docs/plans/2026-08-15` but on hold         | OH-2 more file shapes                                    |
| **Financials**         | USALI mapping dictionary, Summary Operating Statement with drill-through, room inventory, fiscal calendar, occupancy/ADR/RevPAR/TRevPAR with comparisons (OH-6, most of OH-7)                                                                                            | OH-8 budgets, OH-10 GOP→net income/DSCR, OH-11 portfolio roll-up, OH-14 packet export, GOPPAR/CPOR (issue \#26) | OH-9 anomaly detection, OH-13 STAR, OH-20 mapping editor |
| **Labor**              | Schedule 14/15 cost and hours, per-department analytics, scheduling, iPad kiosk with enforced punch order, overtime rules, payroll orchestration to Gusto/ADP with estimate vs actual                                                                                    | —                                                                                                               | OH-15 KPI alerts, OH-12 narrative                        |
| **Tenancy & security** | RLS tenant wall, Keycloak Organizations, client-side sealed PII (ADR-004), per-org field encryption (ADR-005), suppression model (ADR-006), biometric jurisdiction gating (ADR-007)                                                                                      | OH-3 observability                                                                                              | —                                                        |
| **Productisation**     | OH-16 marketing site, OH-17 per-tenant integrations page (QBO OAuth, Gusto/ADP), OH-18 onboarding checklist, invite-gated signup                                                                                                                                         | OH-19 billing, OH-4 CI/CD, open self-service (invite gate lift)                                                 | OH-5 self-host guide, OH-21 chat                         |

**Two naming corrections to make in the repo.** The README and `pms-variants.md` describe the third source as "SkyTouch (Choice / choiceADVANTAGE)". The reports the adapter parses (Final Transaction Closeout, Hotel Journal Summary, Hotel Statistics, Revenue by Rate Code) are the ones Inn-Flow lists for *Choice / choiceADVANTAGE*; Inn-Flow lists *SkyTouch* separately with a different "Back Office Report". SkyTouch Hotel OS is the product Choice's former subsidiary sells to non-Choice hotels; choiceADVANTAGE is Choice's franchise PMS built on the same lineage. Name the source `CHOICEADVANTAGE` and treat SkyTouch as a separate, unbuilt source. Second, `ingestion-contract.md` still says the system "replicates Inn-Flow"; that sentence will be read by every competitor and partner who opens the repo.

## The layer, and what to call it

**Brand systems (franchisor-controlled).** CRS/GRS, loyalty, rate distribution. IHG Concerto, Hilton, Marriott, Choice, Wyndham each own theirs. Only a PMS integrates here.

**PMS (front desk, night audit).** Mandated or approved per brand. Produces the night-audit pack and, on modern platforms, an API or event stream.

**Hotel accounting & labor (Open Hospitality's layer).** Turns the pack into USALI facts, an operating statement, labor cost per occupied room, budgets and variance, reconciliations, alerts. Vendors: M3, Inn-Flow, Otelier, Aptech, Nimble, HelloGM; for labor, Hotel Effectiveness, UniFocus, Deputy. OH is the only open-source entrant and one of two (with Inn-Flow) that do accounting and labor in one system; with the GL decision it becomes, like M3, a vendor that spans this layer and the one below.

**General ledger (system of record for money).** QuickBooks Online, NetSuite, Sage, Business Central. M3 and Aptech are also the GL, which is why they command enterprise pricing and why leaving them is painful. This revision puts OH in this layer too.

The accepted terms, in order of use: "hotel accounting software" (Hotel Tech Report's category, M3's self-description), "back-office platform" (Inn-Flow: "Hotel Accounting and Back-Office Automation Platform"; Otelier: "Back Office Automation"), and "hospitality financial system" (M3, Aptech). No vendor uses "hotel ERP". A useful working phrase for OH is *the accounting and labor layer between the PMS and the ledger*, which says what it is and names the two neighbours it does not try to be.

The long game is a modern, flexible back-office stack with easy onboarding and a broad set of useful integrations, winning the owners the incumbents serve today. The competitive analysis supports that framing with one qualification. Inn-Flow already occupies the "modern, self-serve, integrations" position in owners' minds (it is \#1 on Hotel Tech Report's accounting category with a 4.8 rating and 61 listed integrations), so the differentiation has to be sharper than "modern": open source and self-hostable, labor and accounting on one data model, privacy engineering that is visible to the owner, and published pricing.

## Why not move upstream, and why downstream is now open

Upstream means becoming the PMS, or the front the PMS sits behind. Three reasons not to, in decreasing order of finality.

**The brand gate is closed.** Every major US franchisor either mandates its PMS (Hilton on HotelKey-built PEP, Marriott on Agilysys Stay in North America and OPERA Cloud abroad, Choice on choiceADVANTAGE, Wyndham on SynXis Property Hub for economy and midscale, Accor and Hyatt on OPERA Cloud, BWH on AutoClerk Atlas) or keeps a short approved list (IHG: HotelKey, OPERA Cloud, Shiji). Roughly 72% of US properties are branded. A new PMS cannot sell to any of them until a brand approves it, and IHG has approved two in two years, both from vendors with decades of history.

**The interface burden is the product.** HotelKey's masterlist names about 200 interfaces across 22 categories: eleven payment gateways, twenty lock vendors, forty-odd PBX/call-accounting/voicemail systems, POS emulations for Micros and Simphony, GRMS, IPTV, kiosks, CRS/GDS, RMS. Oracle's document runs to 22 pages of part numbers. Every one is a support obligation. This is what a PMS vendor's engineering budget goes to, and none of it is where OH's value is.

**The independent "headless" route is real but small.** Apaleo and Mews are built to be the reservations engine behind someone else's front, with free sandboxes. That is a legitimate second act for OH with independents, and it does not conflict with the brand route. It is not a way to reach franchisees, whose PMS is fixed by contract.

Downstream is a different case, and this revision opens it. The decision record's "we are deliberately not the GL" and "bank rec: don't build" verdicts were written in July 2026 for a two-property operation with a solo maintainer. Three things have changed the calculus: OH already computes and emits a journal every day, so the posting model exists and merely lacks a home; the cost of building the basics of a ledger in 2026 with Claude Code is a fraction of what it cost the incumbents; and the market evidence in the next section shows that being the GL is exactly what lets M3 and Inn-Flow price as they do and hold customers as they do. The GL section below sets out what "full-featured" means, what it deliberately does not mean, and the order to build it in.

## Competitive analysis, re-verified

| Vendor                                        | Sells                                                                               | PMS ingest (public)                                                                                                                                                 | Scale         | Price signal                                          | What they are best at                                                                                    | Where they are weak                                                            |
|-----------------------------------------------|-------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------|-------------------------------------------------------|----------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------|
| **M3**                                        | GL/AP/AR (Accounting Core), CoreSelect for 1–5 hotels, Labor, Insight BI, OCR AP    | "Any exportable data or API"; HotelKey partner-listed                                                                                                               | 8–9k props    | Quote; "\$150/user/mo" starting point per HotelMinder | Is the GL; AAHOA channel (Blackstone majority, AAHOA co-investor, Aug 2024)                              | Onboarding, exit penalties for small properties, login and pay-stub complaints |
| **Inn-Flow**                                  | Accounting, Labor, Payroll, BI, Facilities, Procurement (Lilo, May 2026), Inventory | 18 PMSs documented: automated email for FOSSE, PEP, Opera, Visual Matrix, SynXis, Chorum; manual for OnQ, Choice, SkyTouch, Lightspeed, AutoClerk; API for HotelKey | 1,000+ hotels | Quote, room-count based                               | Select-service owner UX; the widest documented PMS list; \#1 on HTR accounting                           | Bank-rec duplication, mobile, sign-in; closed, US-only                         |
| **Otelier** (owns HelloGM)                    | DigiAudit, Rec, DigiPay, TruePlan, IntelliSight                                     | OPERA Cloud via OHIP (Oracle Marketplace, Aug 2024); SkyTouch, Visual Matrix; HelloGM↔HotelKey                                                                      | 10k+ hotels   | Quote                                                 | Night-audit repository and reconciliation as products; management-company scale                          | Speed, slow feature cadence, no named AI product; HotelKey only via HelloGM    |
| **Actabl** (ProfitSword, Hotel Effectiveness) | BI/forecast, labor, ops, assets                                                     | Digital Night Audit ingests emailed reports; ProfitSword on Choice and HotelKey lists; HE via PMS APIs                                                              | 12–14k props  | Quote                                                 | Labor outcomes (AI Insights: −13% overtime share across 100+ beta hotels, Sep 2026)                      | Needs M3 alliance for financials; mobile and integration friction              |
| **Aptech**                                    | PVNG AP/GL, Execuvue BI, Targetvue                                                  | Oracle, Stayntouch, Agilysys, Infor, Maestro (files)                                                                                                                | 3,500 props   | "Below average" (HTR)                                 | Full-service management companies                                                                        | Lag, uploads, invoice processing; no HotelKey, no AI                           |
| **Nimble**                                    | Accounting, AP, labor/payroll, AI budgeting                                         | Daily-report import; HotelKey-listed                                                                                                                                | 2,000 props   | Quote                                                 | Small owner portfolios                                                                                   | Thin integrations list, small review base                                      |
| **HelloGM** (Otelier)                         | Owner dashboard: night-audit automation, OTA and deposit reconciliation, flash      | HotelKey pulls (reviewers name it)                                                                                                                                  | —             | Quote                                                 | The single-owner "did the money arrive" question                                                         | Deposit logic confuses users; bank-link timeouts                               |
| **UniFocus / Deputy**                         | Workforce management                                                                | PMS APIs (Oracle, Infor, Agilysys, Mews)                                                                                                                            | —             | Quote                                                 | IHG Approved Vendor status (Jun 2022)                                                                    | Not hotel-accounting; Deputy is generic                                        |
| **Open Hospitality**                          | USALI SOS, Schedule 14/15, scheduling, kiosk, QBO push, payroll orchestration       | Opera and AutoClerk (files, same reports Inn-Flow uses), Choice audit pack                                                                                          | 0 paying      | Philosophy page, no numbers yet                       | Open source; accounting + labor on one model; privacy engineering; self-host; GL planned on a 2026 stack | Three PMSs; no ledger, bank feed, AP, budgets, alerts or billing yet           |

### Findings that should drive priorities

- **Ingestion is still mostly emailed PDFs.** Inn-Flow receives automated night-audit emails from six PMSs and manual uploads from five; ProfitSword's FAQ says direct PMS uploads are "not currently supported". OH's PDF-first ingestion is not behind the market; it *is* the market. The gap is the number of report shapes covered, not the mechanism.
- **HotelKey is the one PMS that is API-first for accounting** in Inn-Flow's list, and it is also the one growing fastest (Hilton PEP, IHG limited-service, G6, ESA, Red Roof, and BWH's AutoClerk Atlas launched Oct 2025 "in partnership with HotelKey"). Both pilot properties are on a path to HotelKey-built systems.
- **Nobody bundles what OH bundles.** Actabl needed an API alliance with M3 to connect labor to financials; Otelier has no labor; M3 and Inn-Flow have labor modules but are closed and US-only.
- **Pricing is opaque everywhere.** Not one vendor publishes a per-property price. A published price is a differentiator by itself.
- **AI claims are converging on the same three things:** invoice OCR/coding, anomaly flags, labor forecasting. OH's OH-9/OH-12/OH-15 cover the second and a narrative; nothing covers the first, and the decision record correctly says to integrate rather than build AP.

## Connectivity: what "80–90% of PMSs" actually means

The denominator matters. STR/CoStar count about 65,000 US hotels, roughly 72% branded. The top ten franchisors account for about half of US properties, and each has mandated or approved a small PMS set. Globally there are roughly 650–700,000 hotels, of which China (~320k) and independent Europe (~200k) run local PMSs that no US back-office vendor covers. So:

| PMS family                                                                                                  | Who runs it                                                                     | Est. US properties       | Financial-data path                                                                                  | OH status             |
|-------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------|--------------------------|------------------------------------------------------------------------------------------------------|-----------------------|
| **HotelKey** (incl. Hilton PEP, BWH AutoClerk Atlas)                                                        | Hilton (all brands), IHG limited-service, G6, ESA, Red Roof, BWH going forward  | 10–15k and rising        | HK API + Datastream; PEP also emails "Final Audit Dynamic", Hotel Statistics, Market Segment Summary | None                  |
| **choiceADVANTAGE**                                                                                         | Choice (all US brands, incl. ex-Radisson Americas)                              | ~5.9k                    | Manual download of the audit pack; Omniboost has a feed                                              | Built (as "SkyTouch") |
| **Marriott FOSSE → Agilysys Stay**                                                                          | Marriott US/Canada (converting 2026–28)                                         | ~5.8k                    | FOSSE: automated emailed reports (NADLYSUM etc.); Stay: APIs + reports                               | None                  |
| **Sabre SynXis Property Hub**                                                                               | Wyndham economy/midscale (5,000+ migrated)                                      | ~5k                      | Automated emailed reports; Hapi connector                                                            | None                  |
| **Oracle OPERA 5 / Cloud**                                                                                  | Hyatt, Accor, Marriott intl, Wyndham and IHG full-service, upscale independents | 5–7k US; 30–40k global   | Opera 5: emailed trial_balance / stat_dmy_seg / manager_report; Cloud: OHIP or the same exports      | Files built OHIP none |
| **Visual Matrix**                                                                                           | Economy/midscale independents, Sonesta preferred, several approved lists        | 2–3k                     | Automated emailed reports                                                                            | None                  |
| **AutoClerk (classic)**                                                                                     | BWH today, independents                                                         | 1–2k, shrinking to Atlas | Manual download                                                                                      | Built                 |
| **Cloudbeds, Mews, Apaleo, Stayntouch**                                                                     | Independents; Mews is Choice-approved outside the US                            | 5–7k US; 35k+ global     | Open APIs with accounting endpoints; Omniboost                                                       | None                  |
| **Long tail** (roomMaster, WebRezPro, ASI, innRoad, RMS, Guestline, SIHOT, protel, eZee, Hotelogix, Shiji…) | Independents by region                                                          | ~15k US                  | Mixed; mostly report export                                                                          | None                  |

Counts are estimates reconciled from franchisor annual reports, vendor claims and Hotel Tech Report; treat each as ±25%. No audited PMS-share-by-property dataset exists.

**What the numbers say.** The first five rows are about 32–36k US properties, roughly half of the country, and OH has two of the five. Adding Visual Matrix and Cloudbeds reaches about 60%. Past that, every point of coverage costs a new parser for a PMS with a few hundred properties. So the objective is fixed at **seven integrations**, reaching about 60% of all US properties and roughly 80% of branded ones, with the independent tail through Omniboost- or Hapi-style aggregators rather than an eighth adapter. Globally, the same seven plus Mews, Guestline, SIHOT and protel cover perhaps 60–70% of branded properties outside China; 80% of all hotels worldwide is not a target any vendor meets.

| \#  | Integration                                                       | Mechanism                                                                              | Status          | Why it is in the seven                                                                      |
|-----|-------------------------------------------------------------------|----------------------------------------------------------------------------------------|-----------------|---------------------------------------------------------------------------------------------|
| 1   | **HotelKey** (incl. Hilton PEP emailed pack, BWH AutoClerk Atlas) | HK API + Datastream; PDF parser for PEP's emailed reports                              | None            | Largest and fastest-growing brand block; API-first; both pilot properties are headed here |
| 2   | **choiceADVANTAGE**                                               | Audit-pack PDF (built as "SkyTouch")                                                   | Built           | All Choice US brands, ~5.9k properties                                                      |
| 3   | **Oracle OPERA** 5 and Cloud                                      | Emailed reports (built); OHIP for Cloud (Oracle Marketplace listing)                   | Files OHIP none | Full-service everywhere; Hyatt, Accor, Marriott international; the Opera pilot property          |
| 4   | **Sabre SynXis Property Hub**                                     | Emailed reports (Statistics, Ledger Comparison, Revenue Recap, Transaction Total List) | None            | Wyndham economy/midscale, ~5k properties, stable mandate                                    |
| 5   | **Marriott** FOSSE → Agilysys Stay                                | Emailed reports (FOSSE); Stay APIs and reports                                         | None            | ~5.8k US properties; build the Stay side as the durable one                                 |
| 6   | **Visual Matrix**                                                 | Emailed reports                                                                        | None            | 2–3k economy/midscale, Sonesta preferred, several approved lists                            |
| 7   | **Cloudbeds**                                                     | REST API with accounting endpoints (tier-gated)                                        | None            | Largest US independent cloud base; the template for Mews/Apaleo later                       |
| —   | AutoClerk classic                                                 | Manual download (built)                                                                | Built           | Already there; migrates to Atlas (#1) over time                                             |

**The good news in the Inn-Flow list.** Six of its eighteen PMSs deliver the same three-report pattern OH already parses (trial balance or transaction summary, manager's report, market segment). OPERA's report names in Inn-Flow's list (`trial_balance`, `stat_dmy_seg`, `manager_report`) are literally the ones in OH's `ingestion-contract.md`. Each new emailed-report source is a parser and a mapping dictionary, not a new architecture; the pack splitter built for Choice generalizes.

## The general ledger

"Full-featured" here means: a hotel owner with one to ten properties can keep the books in OH, reconcile them to the bank, pay vendors, and hand the CPA a year-end package, without QuickBooks. It does not mean replicating QuickBooks. Intuit's surface is a decade of edge cases for every industry; OH's is one industry, one chart of accounts (USALI), and a known set of transaction sources. That narrowing is the whole advantage.

### What changes in the existing decision records

- **`build-vs-integrate.md`, "Bank reconciliation: DON'T BUILD."** Superseded. The verdict rested on two grounds: that QuickBooks does it, and that Pillar E5 was designed so OH could not hold banking credentials. The first is now the competitive target rather than a reason to defer; the second is addressed below.
- **Pillar E5, deposit accounts.** The property to keep is the one that matters: OH never stores an account number, routing number or online-banking credential in a form the server can read. Employee direct-deposit details stay HPKE-sealed client-side. What changes is that OH will hold *aggregator access tokens* (Plaid or Yodlee) that grant read-only transaction access to the *business* operating accounts, stored with ADR-005 per-org field encryption like the QBO refresh token, revocable from the integrations page, with every read written to the audit log. That is a materially different threat model from holding credentials: a token cannot initiate payment, and the aggregator's own consent screen is the credential holder. Write it up as an amendment, not a reversal.
- **The "system of record" line.** The README's "we are the system of record and the accounting brain, not the GL" becomes "the system of record for the hotel's operations and its books". The QBO push stays as an export target for owners whose CPA insists.

### Bank connection: the v1 decision

Plaid and Yodlee (Envestnet) both cover US business checking at the banks small hotel owners use; Finicity (Mastercard) and MX are the other two. For v1 the choice is less about coverage than about three practical things: whether the aggregator supports the small regional banks and credit unions common among franchise owners (Yodlee historically wider; Plaid has closed most of the gap and has better developer tooling); pricing at low volume (Plaid's per-connected-account pricing is public and usable from day one; Yodlee is enterprise-contracted); and OAuth-based connections at the large banks (both now support them, which is what keeps the aggregator, not OH, in the credential path). Recommendation: Plaid Transactions for v1, behind a port so Yodlee can be the second adapter for the bank a real owner turns up with. Statement upload as a fallback only for banks neither reaches, not as the v1 path.

### GL slices, stack-ranked

| \#  | Slice                             | What it delivers                                                                                                                                                                                                                                                                                                | Why in this position                                                                                                                                                                                                                                                                  |
|-----|-----------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| G1  | **Posting core**                  | Chart of accounts seeded from USALI (11th ed.) with per-org extensions; immutable double-entry journal with source links back to staged PMS rows and labor facts; fiscal periods with a close and reopen (audited); trial balance and balance sheet; the existing operating statement re-pointed at the journal | Everything else posts into it. OH already emits this journal to QBO; G1 gives it a home. Reuses the fiscal calendar (OH-6) and the fact tables.                                                                                                                                       |
| G2  | **Bank feeds and reconciliation** | Plaid connection per operating account; daily transaction pull; matching engine against PMS settlements by payment type, OTA payouts, payroll debits, vendor payments; unmatched queue; reconciliation status per account per period                                                                            | The daily reason an owner or bookkeeper opens the books; the module the incumbents built around uploaded statements and that reviews complain about (Inn-Flow: duplicates and "limbo"; HelloGM: deposit logic). It also subsumes the OTA/deposit reconciliation item from Revision 1. |
| G3  | **Accounts receivable**           | City ledger import from the PMS (direct bill, group masters), aging, receipts applied against the bank feed, statements                                                                                                                                                                                         | Hotels already run AR in the PMS; OH mirrors it and closes the loop when the payment lands. Small build, and it completes G2's matching.                                                                                                                                              |
| G4  | **Accounts payable, basic**       | Vendor master, bill entry with USALI line coding (the mapping engine with a new input), approval, due dates, payment recording; OCR capture and bill-pay rails via an integration port (Bill.com, Ramp, Melio) rather than built                                                                                | The module Inn-Flow and M3 lead with. OH owns the coding and the ledger effect; the capture and the money movement are bought. This is the boundary the old decision record drew, kept.                                                                                               |
| G5  | **Multi-entity**                  | Entities as first-class (property LLCs, management company, holding company); intercompany due-to/due-from; consolidated statements; management-fee and shared-cost allocation rules                                                                                                                            | A five-hotel owner is usually five entities. The org-scoped tenancy model already exists; entities are a level under it. Needed before the portfolio roll-up (OH-11) means anything to an accountant.                                                                                 |
| G6  | **Fixed assets and below-GOP**    | Asset register, depreciation schedules, FF&E reserve, loan schedules with interest/principal split; the GOP-to-net-income bridge (OH-10) becomes real entries rather than a report                                                                                                                              | What lenders read; what an owner's refinance pack needs. Reuses OH-10's design.                                                                                                                                                                                                             |
| G7  | **Tax and year-end**              | Occupancy and sales tax liability by jurisdiction from the PMS tax codes already mapped; 1099-NEC data from AP; a CPA export (trial balance, GL detail, fixed assets) in the shapes tax software imports; an accountant role with read-only access                                                              | The CPA is the gatekeeper for "can we drop QuickBooks"; this is what earns their yes. Filing itself stays with the CPA or a filing service.                                                                                                                                           |
| G8  | **Cash and forecasting**          | 13-week cash forecast from AR, AP, payroll runs and the bank balance; owner distributions                                                                                                                                                                                                                       | The feature M3 and Nimble market as "AI"; with G1–G7 it is a query.                                                                                                                                                                                                                   |

**Explicitly out of scope**, so the GL does not become QuickBooks by accretion: inventory and cost accounting beyond F&B COGS lines, payroll tax calculation and filing (the provider does it, per the existing design), point-of-sale, invoicing to guests, multi-currency, and anything a non-hotel business would need.

### Devil's advocate on the GL

**The CPA, not the code, is the adoption gate.** Owners do not pick their ledger; their accountant does, and the accountant has QuickBooks Accountant, a workflow, and no reason to learn a hotel ledger. G7 exists to answer this, but it should be tested early: ask two or three of the owners' CPAs what package they would need to accept OH's books before G1 is finished. If the honest answer is "just give me a QuickBooks file", the QBO export stays a first-class feature for a long time.

**Being the ledger is a liability shift.** When OH is an analytics layer, a mapping bug is an annoyance. When OH is the books, a posting bug is a misstatement and a bank-token breach is a regulatory event. The engineering answer is the immutable journal, the period close and the audit log in G1; the business answer is the SOC 2 timeline, cyber insurance and a legal entity, which move earlier in the readiness list.

**It competes for the same year as connectivity.** Seven PMS integrations and a GL are each a year of work for a small team. The sequencing below interleaves them so that G1 lands early (it is small and everything depends on it) while G2 waits for the first real bank connection at a pilot property, which is also the first test of the E5 amendment.

## Stack-ranked features

Ranked by expected contribution to the first cohort of paying tenants given the seven-integration target and the GL decision, with dependencies explicit. "Roadmap" cites the existing catalogue id or the proposed one.

> **Adopted 2026-09-06 with one amendment.** `docs/ROADMAP.md` carries this ranking with subscription billing (\#13 below) moved to the top of Tier 1: the roadmap's premise is a paying tenant, and the open-core boundary decision billing forces must precede the hosted bank connection shipping. Where the two orderings differ, `docs/ROADMAP.md` is the one in force.

### Tier 0 · the spine: first feed, the compliance gate, the ledger core

#### 1. HotelKey adapter: HK API + Datastream, plus PEP's emailed audit pack (OH-22)

**Why first:** Integration \#1 of seven. The largest and fastest-growing brand block in the US, the only API-first accounting feed among the brand PMSs, and the platform both pilot properties are heading toward. Credentials are property-initiated and already requested.

**Scope:** Two paths behind one source: API/Datastream where the property grants credentials; a parser for PEP's emailed "Final Audit Dynamic", Hotel Statistics and Market Segment Summary for Hilton properties where the API is franchisor-gated. Include the settlement-by-payment-type feed from day one; G2 needs it.

**Depends on:** HotelKey credentials (in flight); \#7 before the second HotelKey property.

#### 2. Ingestion-boundary redaction on the authenticated path (ROADMAP §2.4)

**Why here:** The gate that lets a stranger upload a real audit pack; the first line on every security review; the basis of "cardholder data is redacted at ingestion and never stored". More important, not less, once OH holds bank tokens.

**Depends on:** Nothing. Smallest item on the list.

#### 3. GL posting core (G1) (new · OH-27)

**Why here:** Every later slice posts into it, and it is smaller than it sounds: the fiscal calendar, the USALI dictionary, the staged facts and the QBO journal generator exist. Building it now means the bank feed (#5), AR, AP and the below-GOP work all land on one immutable journal instead of each inventing its own. Ship the QBO push as an export *from* the journal, so nothing owners rely on today breaks.

**Depends on:** A short ADR: chart-of-accounts model (USALI base plus per-org extensions), period close semantics, and the immutability rule.

#### 4. Emailed-report intake: a mailbox per property, auto-routed (new · OH-23)

**Why here:** Four of the seven target PMSs deliver by scheduled email. Until OH has an inbound address per property with detection-registry routing, each of them is a daily manual upload, and "self-service onboarding" is a slogan. Bank and card-processor statements can arrive the same way as a fallback.

**Depends on:** \#2.

### Tier 1 · the ledger becomes the books; the seven integrations fill in

#### 5. Bank feeds and reconciliation (G2) via Plaid (new · OH-28)

**Why here:** The feature that makes OH the books rather than a report about them, and the one where the incumbents' own reviews show weakness. Start with the pilot properties' own operating accounts: it is the first live test of the E5 amendment and of the matching engine against real PMS settlements, OTA payouts and payroll debits. Absorbs Revision 1's deposit/OTA reconciliation item.

**Depends on:** \#3; the E5 amendment as an ADR; Plaid production access (needs the legal entity); settlement feeds from \#1 and the built sources.

#### 6. SynXis Property Hub and Visual Matrix parsers (integrations \#4 and \#6) (OH-2)

**Why here:** Both stable, both emailed, both with report names published by Inn-Flow; together 7–8k US properties. Cheapest coverage per parser once \#4 exists. Marriott (#5 of seven) waits for the Agilysys Stay reports to settle, unless a Marriott owner turns up first.

**Depends on:** \#4; sample packs from real properties, sourced through the owner network.

#### 7. Mapping editor, after the dictionary-tenancy decision (OH-20)

**Why here:** Choice codes are franchise-configurable, HotelKey's will be, and once OH is the ledger a wrong mapping is a misposted entry, not a misfiled statistic. Base-plus-override is recommended. `MappingException` and `CoveragePage` are the worklist and the read side already.

**Depends on:** The schema decision, recorded as an ADR.

#### 8. Accounts receivable and accounts payable, basic (G3, G4) (new · OH-29)

**Why here:** AR is a small mirror of the PMS city ledger that completes bank matching. AP is the incumbents' headline module; OH builds the vendor master, USALI coding and approval, and buys capture and payment rails through a port. Together they are what an owner means by "my books".

**Depends on:** \#3, \#5.

#### 9. Budget import and variance (OH-8)

**Why here:** The first column every owner, CPA and lender reads, and the first thing a side-by-side demo against any incumbent gets judged on. Unchanged from Revision 1 except that budgets now attach to GL accounts, which is simpler than attaching them to report lines.

#### 10. Night-audit repository with sign-off (new · OH-24)

**Why here:** The wedge product one incumbent built a business on; OH already receives the packs. Drops from Tier 1's top in Revision 1 because the bank feed now supplies the daily reason to open OH. Still cheap, and still a credible alternative to the standalone night-audit repository products.

**Depends on:** \#2, \#4.

### Tier 2 · completing the seven and the ledger; the modern stack

#### 11. OPERA Cloud via OHIP on Oracle Cloud Marketplace, and Cloudbeds API (integrations \#3 and \#7) (OH-22)

**Why here:** Opera files already work, so OHIP is an upgrade and a channel listing; Cloudbeds is the independent-segment template and its API is tier-gated on the hotel's side. Both are API projects that benefit from \#1 having settled the API-source pattern.

**Depends on:** Legal entity (OPN membership, Cloudbeds partner agreement).

#### 12. Multi-entity, fixed assets and year-end (G5, G6, G7) (new · OH-30)

**Why here:** What turns "the books for a hotel" into "the books for a hotel company" and earns the CPA's sign-off to leave QuickBooks. Absorbs OH-10 (GOP to net income, DSCR) and gives OH-11 (portfolio roll-up) an accounting meaning. Test the CPA package with real accountants before building G7.

**Depends on:** \#3, \#5, \#8.

#### 13. Subscription billing and the open-core line (OH-19)

**Why here:** Greenfield; turns a tenant into revenue. The open-core question gets sharper with a GL: the ledger core and the PMS adapters are what make the Apache-2.0 core useful to a competitor, and the hosted bank connection (aggregator contracts, SOC 2, insurance) is the natural paid line. Decide before \#5 ships.

#### 14. Notification port with Slack first, suppression-aware (new · OH-26)

**Why here:** The delivery plumbing OH-15 and OH-9 lack, and now the channel for "three unmatched bank transactions over \$500" and "period ready to close". Same disclosure discipline as before; per-tenant recipients needed first.

#### 15. Integration marketplace: monday.com, CRM demand feed, more payroll providers, Yodlee as second bank adapter (OH-17 extension)

**Why here:** Each is an adapter behind the existing field-spec pattern; each is a standing DPA and maintenance cost. One at a time, mock first, when a real hotel asks.

#### 16. Cash forecasting (G8), narrative and chat (OH-12 · OH-21)

**Why last:** With the ledger, bank feed, AR, AP and payroll runs in one system, the 13-week cash forecast is a query and the narrative has real material. Keep the rule: numbers are computed, prose never introduces a figure.

### What is deliberately not on the list

- **AP capture, OCR and bill-pay rails.** Bought through a port (Bill.com, Ramp, Melio); OH owns coding and the ledger effect.
- **Payroll tax calculation and filing.** The provider's, per the existing design.
- **A PMS, a booking engine, or a front-desk UI.** See "Why not move upstream".
- **Inventory, multi-currency, guest invoicing, POS.** The QuickBooks-by-accretion risks.
- **An eighth PMS parser before the seven are done.** Aggregators for the tail.

## Proposed roadmap.yml entries

Phrased as user-facing capabilities so the triage bot can dedup against them. Ids continue the catalogue. Applied to `.github/roadmap.yml` on 2026-09-06.

| id      | title                                                 | summary (user-facing)                                                                                                                                                                                                                                              | status      | tags                             |
|---------|-------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------|----------------------------------|
| `OH-23` | Emailed night-audit intake                            | Each property gets its own inbound email address; the PMS's scheduled night-audit reports are received, redacted, recognized and processed automatically, with no daily upload.                                                                                    | planned     | ingestion, onboarding            |
| `OH-24` | Night-audit report repository                         | Every received audit pack is kept (redacted), searchable by business date and report, with a record of who reviewed it and when.                                                                                                                                   | planned     | reporting, compliance            |
| `OH-26` | Notification delivery (Slack, email, SMS)             | Per-property recipients and channels for alerts, digests and approvals, honoring the same disclosure rules as the API.                                                                                                                                            | planned     | integrations, notifications      |
| `OH-27` | General ledger: posting core                          | Keep the books in Open Hospitality: a USALI chart of accounts, a double-entry journal every PMS and labor fact posts into, fiscal periods with a close, trial balance and balance sheet. QuickBooks becomes an optional export.                                    | planned     | gl, accounting                   |
| `OH-28` | Bank feeds and reconciliation                         | Connect each operating bank account (Plaid, later Yodlee), pull transactions daily, match them to PMS settlements, OTA payouts, payroll and vendor payments, and work an unmatched queue to a reconciled period. No statement uploads, no stored bank credentials. | planned     | gl, reconciliation, integrations |
| `OH-29` | Receivables and payables                              | Mirror the PMS city ledger with aging and receipts, and record vendor bills coded to USALI lines with approvals and due dates; capture and payment rails through a connected provider.                                                                             | planned     | gl, ap, ar                       |
| `OH-30` | Multi-entity books, fixed assets and year-end package | Entities per property and company with intercompany balances and consolidation; asset register, depreciation, FF&E and loan schedules; occupancy/sales tax liability and a CPA year-end export with an accountant role.                                            | considering | gl, accounting, reporting        |

Existing entries to touch: `OH-22` should name HotelKey, OHIP and Cloudbeds as its targets; `OH-2` should name SynXis Property Hub, Marriott FOSSE/Agilysys Stay, Visual Matrix and Hilton PEP's emailed pack; `OH-10` and `OH-11` should reference `OH-30`; the README's "SkyTouch" becomes choiceADVANTAGE and its "not the GL" line is rewritten. Two ADRs before code: the E5 amendment for aggregator tokens, and the GL posting model.

## Verification log

Sixteen claims from the 3 September brief were re-checked against primary sources on 6 September 2026.

| Claim                                                                                                                   | Result    | Note                                                                                            |
|-------------------------------------------------------------------------------------------------------------------------|-----------|-------------------------------------------------------------------------------------------------|
| IHG approved HotelKey as first cloud PMS for US/Canada limited-service, Nov 22 2024; 250 by end-2024, 1,500 by end-2025 | Verified  | Hospitality Net release                                                                         |
| Oracle OPERA Cloud approved by IHG for Americas and EMEAA, Jan 29 2026, as a franchisee choice                          | Verified  | "Optional" is a paraphrase of "enabling franchisees … to make tech decisions"                   |
| IHG FY2025: cloud PMS at 2,000 hotels, 4,000 target for 2026; Shiji in Greater China                                    | Verified  | In the full results PDF, not the web summary                                                    |
| UniFocus and Deputy IHG Approved Vendors, June 2022; Christian Urbat, Head of Owner Solutions                           | Verified  |                                                                                                 |
| IHG Owners Association Allied tiers \$2,500–\$70,000                                                                    | Verified  | 2025 EMEAA kit; a 2026 kit exists and was not price-checked; US pricing unpublished             |
| OHIP production path: OPN Level 0, Marketplace listing, notify OHIP; \$500 OPN fee                                      | Verified  | Fee is from the OPN FAQ, not the OHIP doc                                                       |
| Otelier on Oracle Cloud Marketplace via OHIP, Aug 2024                                                                  | Verified  |                                                                                                 |
| Hapi–HotelKey Data Streams, Jul 31 2025                                                                                 | Verified  | A Hapi customer story, not a press release                                                      |
| HotelKey integrations page lists M3, ProfitSword, HelloGM; Inn-Flow's ServiceNow/credential process                     | Verified  | Inn-Flow itself is not on HotelKey's public page despite integrating                            |
| Blackstone majority stake in M3 with AAHOA, Aug 2024                                                                    | Verified  |                                                                                                 |
| Inn-Flow acquired Lilo, May 2026                                                                                        | Verified  |                                                                                                 |
| Actabl AI Insights −13% overtime share, 100+ hotels, Sep 2026                                                           | Verified  | Beta properties since June 2026                                                                 |
| Hilton PEP replacing OnQ across ~7,000 hotels, Apr 2023                                                                 | Partly    | Hilton's release never says "OnQ"; the replacement framing is trade press                       |
| ProfitSword ingests by scheduled email or manual upload only                                                            | Verified  |                                                                                                 |
| HotelKey founded 2014/15, Carrollton TX, 25,000+ contracted / 15,000+ live                                              | Partly    | Founded 2014 per About page; HQ city only in third-party directories; counts are company claims |
| "Roughly 500 staff" at HotelKey                                                                                         | Withdrawn | GetLatka estimate (514) conflicts with HTR (150); no primary source                             |

**Corrections to the earlier brief.** BWH's AutoClerk Atlas (Oct 2025) is built with HotelKey, which strengthens the HotelKey case and was missing. Wyndham's economy and midscale hotels run Sabre SynXis Property Hub, not OPERA, and Marriott US is moving to Agilysys Stay; both were absent from the earlier PMS map. The earlier brief said "SkyTouch/choiceADVANTAGE" as one thing; Inn-Flow treats them as two.

**Not researched in this revision.** Plaid vs Yodlee coverage of small regional banks and credit unions, current Plaid business-account pricing, and each aggregator's support for the specific banks the pilot properties use. Verify against the aggregators' institution lists before the E5 amendment is finalized.

**Still unverified.** Whether IHG or HotelKey permit third-party API access to IHG-branded HotelKey properties through Hapi rather than a property ticket; whether HotelKey met the 1,500-property IHG target; IHG's security questionnaire and any approved-vendor rebate; every property-count estimate in the connectivity table.

## Sources

- Supplied: HotelKey Platform Integrations and Third-Party Interfaces masterlist (Aug 2026); Oracle OPERA 5 / Cloud / Suite8 Documented Interface Configurations G48597-02 (Mar 2026)
- Repo at `5b01df5`: `README.md`, `docs/ROADMAP.md`, `.github/roadmap.yml`, `docs/reference/pms-variants.md`, `build-vs-integrate.md`, `sources/ingestion-contract.md`, `docs/plans/2026-08-15-pms-hotelkey-skytouch.md`, `src/usali/adaptors/`
- [Inn-Flow: Night audit reports and integrations (18 PMSs, delivery method, report names)](https://support.inn-flow.net/support/solutions/articles/1000322695-night-audit-reports-and-integrations) · [Inn-Flow: HotelKey API setup](https://support.inn-flow.net/support/solutions/articles/1000324500-hotelkey-how-to-setup-the-hotelkey-api-)
- [IHG selects HotelKey (Nov 2024)](https://www.hospitalitynet.org/news/4124804.html) · [Oracle approved by IHG (Jan 2026)](https://www.oracle.com/news/announcement/oracle-approved-by-ihg-hotels-and-resorts-as-a-property-management-provider-2026-01-29/) · [IHG FY2025 results](https://www.ihgplc.com/~/media/Files/I/Ihg-Plc/results/2026/full-year-2025/full-year-results-for-the-year-to-31-dec-2025.pdf)
- [BWH AutoClerk Atlas with HotelKey (Oct 2025)](https://lodgingmagazine.com/bwh-hotels-announces-launch-of-autoclerk-atlas-property-management-system/) · [Hilton PEP (Apr 2023)](https://stories.hilton.com/releases/hotel-key-partnership) · [HotelKey About](https://www.hotelkeyapp.com/about-us) · [HotelKey integrations](https://www.hotelkeyapp.com/integrations)
- [Wyndham on SynXis Property Hub](https://hoteltechreport.com/news/sabre-hospitality-renews-wyndham-following-accelerated-migration) · [Wyndham full-service on OPERA Cloud](https://www.hotelmanagement.net/tech/wyndham-rolls-out-oracle-pms-full-service-hotels) · [Marriott "Power of M" to Agilysys Stay](https://profitswordhelp.zendesk.com/hc/en-us/articles/47392108964123-What-to-Expect-with-the-Power-of-M-PMS-Transition) · [Accor on OPERA Cloud](https://www.oracle.com/news/announcement/oracle-hospitality-opera-cloud-selected-by-accor-for-global-pms-2025-09-04/) · [Hyatt on OPERA Cloud](https://skift.com/2024/09/17/hyatt-continues-shift-to-cloud-for-1000-hotels/) · [Choice approves Mews outside US](https://www.mews.com/en/press/choice-hotels-international-mews)
- [US hotel census by chain scale (CoStar/STR)](https://www.mmcginvest.com/post/the-hospitality-market-by-chain-scale-a-complete-industry-analysis) · [Cloudbeds 2025 independent lodging report](https://www.cloudbeds.com/articles/2025-independent-lodging-report/) · [US properties by chain](https://www.withorbital.com/data/largest-hotel-chains-in-the-us/)
- [Hapi 14,000 hotels](https://hotelbusiness.com/hb-exclusive-hapi-surpasses-14000-connected-hotels/) · [Hapi × HotelKey](https://www.stayhapi.com/resources/bringing-hospitality-tech-together-the-hotelkey-and-hapi-integration-story) · [Omniboost](https://omniboost.com/media/omniboost-announces-integration-with-opera-cloud-and-opera-on-prem-pms-systems) · [HTNG Express](https://hoteltechreport.com/news/hapi-ahla-htng-express)
- [Oracle OHIP partners to production](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/t_partners_moving_to_production_ocim.htm) · [OPN FAQ](https://www.oracle.com/partnernetwork/program/faq/) · [Otelier on Oracle Cloud Marketplace](https://www.hospitalitynet.org/news/4123137.html)
- [UniFocus IHG Approved Vendor](https://www.hospitalitynet.org/news/4110834.html) · [Deputy IHG Approved Vendor](https://news.deputy.com/deputy-is-selected-by-ihg-hotels--resorts-as-approved-vendor-for-workforce-management) · [IHGOA Allied Member kit](https://www.owners.org/sites/default/files/documents/2025_emeaa_allied_member_kit_f241016.pdf)
- [Blackstone / M3](https://www.blackstone.com/news/press/blackstone-acquires-majority-stake-in-leading-hotel-accounting-software-and-services-provider-m3/) · [Inn-Flow / Lilo](https://www.hospitalitynet.org/news/4132197/inn-flow-acquires-lilo-to-unify-procurement-with-accounting-and-labor-accelerating-ai-driven-innovation-across-its-hotel-platform) · [Actabl AI Insights](https://www.hospitalitynet.org/news/4134178/actabls-ai-insights-cut-overtime-share-of-hours-by-13-across-100-plus-hotels) · [ProfitSword DNA FAQ](https://profitswordhelp.zendesk.com/hc/en-us/articles/46988072368667-PS-Digital-Night-Audit-FAQ)
- Hotel Tech Report: [Hotel Accounting Software category](https://hoteltechreport.com/operations/hotel-accounting-software), [M3](https://hoteltechreport.com/operations/hotel-accounting-software/m3-accounting), [Inn-Flow](https://hoteltechreport.com/operations/hotel-accounting-software/inn-flow-accounting), [Aptech](https://hoteltechreport.com/operations/hotel-accounting-software/aptech-pvng), [HotelKey](https://hoteltechreport.com/operations/property-management-systems/hotelkey-pms) · [HotelMinder M3 pricing](https://www.hotelminder.com/partner=M3) · [HelloGM reviews](https://www.capterra.com/p/218506/HelloGM/reviews/)
