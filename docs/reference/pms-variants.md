# Hotel PMS variants — integration & ingest reference

Research 2026-08-01 (web: vendor docs, developer portals, Hotel Tech
Report, industry press). Feeds the Onboarding Flow PMS touchpoint
(`docs/design/2026-08-01-onboarding-flow-design.md` §4). Install-base
figures marked "(vendor claim)" are directional, not verified.

Already supported by file-export ingest: **Oracle OPERA**, **AutoClerk**.

## Summary table (roughly most-common-first, US/independent)

| Product (vendor) | Segment | File export | API | Notes |
|---|---|---|---|---|
| **Oracle OPERA** (5 / OPERA Cloud / OHIP) | Enterprise/chain (+ many independents on legacy Opera 5) | Yes — report writer CSV/XML/PDF, Export APIs | REST via **OHIP** (3,000+ endpoints, webhooks) | Built. OHIP production access flows through the customer's OPERA Cloud tenant or Oracle Store partnership — not cold anonymous signup |
| **AutoClerk** (Agilysys) | Independent/boutique, small chains | Yes (primary path) | No public API | Built |
| **Cloudbeds** | Independent/mid-market (40,000+ claim) | Yes — XLSX/PDF/CSV/JSON, dedicated Night Audit | REST incl. Accounting & Data Insights APIs, Marketplace | API can be a **paid/package-gated** add-on — request from account |
| **Mews** (Mews OS) | Independent/mid-market/lifestyle | Yes | **Open API** family, free public sandbox | Most self-service: build/test free, only production needs certification |
| **Apaleo** | Boutique/tech-forward, EU-heavy growing US | Via API | **API-first — the API is the product**, free registration, OAuth2 | Best self-serve dev UX; any dev registers free |
| **RMS Cloud** | Independent/mid-market, resort/VR (~7,000 props) | Reports/exports | REST API | Activation via `apisupport@rmscloud.com` — partner-mediated |
| **roomMaster / roomMaster Anywhere** (InnQuest) | Independent hotels/motels/B&Bs | Yes — 270+ reports, SQL builder | Open API, 100+ integrations | Large legacy install base |
| **ASI FrontDesk** (Anand Systems) | Independent motels — small/economy (4,600+ claim) | Yes | Limited/unclear | Large under-the-radar independent-motel base |
| **Visual Matrix** | Independent/economy motels, Wyndham/Choice economy tiers (3,000+ claim) | Yes | First **HTNG Express PMS API** implementer | High priority — big US economy-motel base |
| **WebRezPro** (World Web Tech) | Independent inns, small hotels, B&Bs | Yes — Excel, scheduled email export | API access, 150+ integrations | Solid file target |
| **eZee Absolute** (Yanolja) | Budget/independent, strong Asia/ME, growing US | Yes — PDF/Excel/CSV | XML/API (3rd-parties call it "unofficial") | |
| **Hotelogix** | Independent budget/mid, India/APAC + US | Yes — 100+ reports | Open API | |
| **SkyTouch (choiceADVANTAGE)** | Choice Hotels franchise-mandated (6,000+ claim) | Auto night audit, reporting | **/CONNECT** API platform | Brand-locked; /CONNECT is real & registerable |
| **HotelKey** | Independent + Hilton migrating all 7,000 props by ~2026 | Built-in reporting, auto night-audit sync | API + Hapi Data Streams | Fast-rising priority (Hilton scale) |
| **Agilysys** (Visual One / Stay / LMS) | Full-service/resort/casino, upper-mid→enterprise | Yes via Agilysys Analyze | RESTful (esp. Stay) | Same parent as AutoClerk |
| **Infor HMS** | Enterprise/branded, full-service | Standard reporting | Open APIs, HTNG, 700+ integrations | |
| **Stayntouch** | Boutique/lifestyle/select-service, mobile-first | Scheduled export (incl. EOD/night audit) | Open "Connect" API, 1,400+ (claim) | |
| **Maestro PMS** (Northwind) | Independent resort/conference, multi-property (NA) | Flexible reporting | Open API; also via **Hapi** | |
| **protel / Planet** (Planet PMS / Protel Cloud) | Independent/mid, EU-heavy | Reporting | Upgraded Open API (2026), 1,200+ integrations | |
| **Clock PMS+** (Clock Software) | Independent/boutique, EU-heavy | Yes — every report CSV/XLS | Full API (XML/JSON), `api_user`/`api_key`, public docs | Genuinely accessible — good adapter template |
| **RDPWin** (Resort Data Processing) | Vacation rental/timeshare/condo/RV (500+ claim) | 600+ Crystal Reports, export/email/automate | PMS API line | |
| **innRoad** | Small independent hotels/B&Bs | 20+ reports (daily flash) | **No public API** (2026) | File export only |
| **ThinkReservations** | Small independent inns/B&Bs | Night Audit module, Reports | 16 partners; no broad public API | QuickBooks journal-entry sync |
| **Little Hotelier** (SiteMinder) | Very small B&Bs/guesthouses | Limited native reporting | Via SiteMinder pmsXchange — but **ARI excluded** | Harder target |
| **RoomKeyPMS** | Independent/mid (CA/US) | Standard reports | REST API | |
| **Guesty** / **Hostaway** | Short-term / vacation rental | Reporting dashboards | Direct APIs (Airbnb/Vrbo/Booking) | Different accounting model (STR, not USALI night-audit) — lower priority |
| **Amadeus** (iHotelier CRS, HotSOS; PMS now via Shiji Daylight) | Enterprise/branded | Enterprise reporting | 200+ API integrations | Amadeus repositioned legacy PMS; partners Shiji for PMS |
| **Sabre SynXis** (CRS + SynXis PMS for small props) | Enterprise/chain (mostly CRS) | Data Warehouse feed | SOAP/REST, HTNG `OTA_HotelResNotifRQ` | Primarily CRS, not a night-audit source |
| **Shiji** (Infrasys POS, ReSA, Daylight PMS) | Enterprise, APAC + Amadeus tie-up | Via Shiji reporting | API-first; also via Hapi | |
| **Springer-Miller SMS\|Host** | Luxury resort/spa/multi-amenity | Standard reporting | Open API + XML, 200+ (claim) | |
| **Hilton OnQ** (legacy → HotelKey) | Hilton-only, being replaced | | | Transitional — target HotelKey |
| **Marriott FOSSE/MARSHA** (legacy → Agilysys) | Marriott-only | | | Legacy TPF mainframe; Agilysys modernization 2022+ |
| **Wyndham** | Full-service → OPERA Cloud; economy franchisees choose from approved list (Visual Matrix, RDP, roomMaster, ASI…) | depends | depends | Not one vendor — route via OPERA + approved-vendor list |

## Modality grouping

**File-export-friendly (fit today's PDF/report ingest):** OPERA,
AutoClerk, roomMaster, WebRezPro, eZee, Hotelogix, RDPWin, Clock PMS+,
ASI FrontDesk, Stayntouch, innRoad, ThinkReservations, Visual Matrix.

**API-first / open dev platforms:** Apaleo (most open), Mews (free
sandbox), Cloudbeds (paid-tier-gated), protel/Planet, Infor HMS,
Agilysys Stay, Springer-Miller, HotelKey, Shiji.

**Closed / hard to self-serve:** choiceADVANTAGE/SkyTouch (Choice-locked),
Hilton OnQ→HotelKey (Hilton), Marriott (brand), Little Hotelier (ARI
excluded), RMS Cloud (mailbox activation), innRoad (no API).

## Top ~15 to build first (US independent/mid-market self-service)

1. Cloudbeds — largest US independent cloud base (API tier may be paid)
2. Mews — #1-voted 2026, fastest-growing US boutique, open API
3. Visual Matrix — large economy-motel base, HTNG Express native
4. roomMaster/roomMaster Anywhere — independent hotel/motel/B&B leader
5. ASI FrontDesk — large under-the-radar independent-motel base
6. WebRezPro — independent inn/small-hotel, clean Excel/CSV
7. RMS Cloud — VR/resort/independent, API gated but reachable
8. Maestro PMS — independent resort/conference (NA)
9. Apaleo — small US base but easiest true self-serve API; good template
10. HotelKey — scaling fast (Hilton migration + independents)
11. SkyTouch/choiceADVANTAGE — Choice-brand density, real /CONNECT API
12. RDPWin — VR/condo-hotel niche, 600+ report templates
13. Clock PMS+ — small US base but excellent open docs (CSV + API)
14. eZee Absolute — large global independent/budget, growing US
15. innRoad + ThinkReservations (one effort) — small-inn/B&B, pure file
    export, matches AutoClerk exactly

## Integration standards

- **HTNG Express PMS API** — lightweight, OpenTravel-schema-based;
  meant to cut PMS-integration from months to days. Visual Matrix is the
  first implementer. Most promising "one adapter, many PMS" bet, but
  adoption is early/vendor-by-vendor → *watch/target*, not a foundation.
  (No current "HTNG NightAudit" standard exists — treat as superseded.)
- **OpenTravel (OTA)** — XML schema under most HTNG messages (e.g.
  `OTA_HotelResNotifRQ`); common wire format, not a night-audit standard.
- **Hapi** — commercial PMS-agnostic connectivity layer (Maestro,
  HotelKey, Shiji). Paid shortcut to multi-PMS API coverage; evaluate vs.
  building API adapters one-by-one.

## Caveats

- Brand-mandated systems aren't "one PMS" — adapter targets the
  underlying engine (SkyTouch/HotelKey/Agilysys/Oracle), not the brand.
- Cloudbeds/RMS self-serve API gating unconfirmed — signup-test before
  committing engineering.
- "Skyware" could not be confirmed as a current hotel PMS — drop unless a
  specific reference surfaces.
