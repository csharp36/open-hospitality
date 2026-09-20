# OH-22 — HotelKey ingestion (design)

Status: **DECIDED (2026-09-20) — IMPLEMENTED on branch feat/oh22-hotelkey; PR number below** — §8 records the review outcome; D-OH22.6
carries the one decision that changed the scope. Scoped deliberately: the
sample set in hand does not include a HotelKey night audit pack, so the
trx-code-grain financial path this repo's other sources are built on cannot be
specified yet. §2 decides what the samples DO support; §3 states what is
blocked and on what. Implementation plan:
[`../plans/2026-09-20-oh22-hotelkey.md`](../plans/2026-09-20-oh22-hotelkey.md).

OH-22 is Tier 0 #1 in [`ROADMAP.md`](../ROADMAP.md). It inherits the ingestion
contract unchanged — detect, stage, transform, promote, with mapping YAML as the
only place a vendor code becomes a USALI classification.

## 1. What actually arrived, and what it is

Five sample reports, property-initiated, received 2026-08-14. They are
**mock data, cleared by the sender for sharing**, and they live outside this
repo at `~/Desktop/Sample Hotel/HotelKey/` — like every other real-format
sample, they are never committed (see the repo's sample-handling rule).

| File | Format | Grain |
|---|---|---|
| `Hotel Statistics - HK.pdf` | PDF, 3pp | Day totals + MTD/YTD, many sections |
| `Settlement By Payment Type.xlsx` | XLSX | **Transaction** (folio, room, card, amount) |
| `All Payments.xlsx` | XLSX | Payment-type totals |
| `AR Invoice Aging.xlsx` (×2, one a duplicate) | XLSX | Aging bucket × transaction type, and × date |

Two facts about this set constrain everything below.

**They are not one property.** The PDF is `Summit Lodge Redstone, TX / RDQSM`;
the spreadsheets are `Lakeview Inn and Suites Bluewater Falls / MQBWF`. They are
format samples from different mock tenants, not a day that can be footed. The
choiceADVANTAGE work could prove itself by reconciling a real pack to the cent
(the OH-27 parity run, `2026-09-07-oh27-sos-cutover-decision.md` §3); **no
equivalent proof is available here**, and no test written against these files
can claim one. This is the difference between "the parser reads the format" and
"the numbers are right", and only the first is reachable today.

**Four of the five are XLSX.** The ingestion pipeline is PDF-only end to end —
`adaptors/pdf.py` and `adaptors/pack.py` are the only readers, every one of the
eight registered adapters takes `list[Word]`, `process_file` hashes and files a
PDF, and `openpyxl` is not a project dependency. HotelKey is the first source
that needs a **second file format**, not just a ninth adapter. §2's D-OH22.2
decides that shape; it is the largest single piece of work in this slice.

## 2. Decisions log

**D-OH22.1 — `HOTELKEY` becomes a registered `pms_source`, but the
`HOTEL STATISTICS` signature must be disambiguated first.**

`detect._REPORT_SIGNATURES` maps an uppercased title phrase to one
`(pms_source, report_type)` pair, first substring match wins. SkyTouch already
owns `"HOTEL STATISTICS"`. **HotelKey's statistics report is titled exactly
`Hotel Statistics`.** The table's core assumption — that a report title names
its vendor — is false the moment two vendors pick a generic name, and this is
the first case.

The collision has two different behaviors, and only one of them is safe:

- **Registered property: fails closed, loudly.** `detect()` compares the
  registry row's `pms_source` against the signature's and raises
  `property … is registered for …, but the report looks like …`. A HotelKey
  property would simply never be able to ingest its statistics report.
- **Anonymous `/try` preview: misroutes silently.** The preview calls
  `detect_report_signature()`, which has no registry and therefore no
  cross-check. A HotelKey statistics PDF is handed to
  `skytouch_hotel_statistics`, which locates columns by shape (one business
  date, two PTD, two YTD). HotelKey's shape is *Actual Today / M-T-D / LY-M-T-D
  / Y-T-D / LY-T-D* — five columns, two of them last-year. Whether that raises
  or returns wrong numbers under a SkyTouch label is unproven either way, and
  the marketing front door is where it would show.

This is the same class of defect `fix/detect-match-section-title` fixed (a
`Rate Plan` column heading routing four unrelated SkyTouch reports to the
AutoClerk adapter): a substring is evidence about text, not identity.

**Decided:** the signature table gains a discriminator rather than a longer
phrase. A title alone cannot separate these two reports, because they are the
same title. The header window can: HotelKey stamps `Report Run Date:` /
`Report Run Time:` / `User:` in the top-right of every report, and prints the
property code on line 2. Signature matching becomes *title phrase, plus an
optional vendor-anchor predicate checked against the header window*.

Rejected: (a) making the phrase longer — there is no longer phrase, the titles
are identical; (b) ordering the table so HotelKey wins — the loser then breaks,
and first-match-wins on an unordered list is the bug, not the fix;
(c) disambiguating by registered property only — leaves the anonymous preview
misrouting, which is the one path with no property to check against.

`supported_pms_sources()` derives the signup dropdown from this table, so
HotelKey appears to signing-up operators the moment it registers. It must not
register until §3's blocked path is resolved or the dropdown will advertise a
source that cannot ingest a night audit.

**D-OH22.2 — XLSX ingestion is a reader seam beside the PDF reader, not a
parallel pipeline.**

`process_file` becomes format-dispatching on content (magic bytes, not
suffix — the night-audit work already established `%PDF` magic-checking as the
idiom). A `adaptors/xlsx.py` yields the same normalized cell stream the
adapters consume, so detection, staging, transform, promotion, coverage and the
audit trail are all reached unchanged. `openpyxl` joins the dependency set
(read-only, `data_only=True`).

The HotelKey spreadsheets make this cheap: all four share one layout — property
name and code in rows 1–2, run metadata rows 2–4, report title row 8, a section
label row, a header row, then numbered detail rows, terminated by
`END OF REPORT`. That is a single parser with a section splitter, closely
analogous to `pack.split_pack`.

Rejected: a separate XLSX-only path that writes facts directly. It would
duplicate staging and coverage recording, and the repo's ingest history is
mostly the cost of things that skipped staging.

**D-OH22.3 — the statistics report is the first HotelKey adapter, and it
carries more than statistics.**

`Hotel Statistics` is not the thin report its name suggests. Its sections:

- *Room Statistics* — total/clean/dirty/OOO, rooms available, stay-overs,
  house rooms, rooms sold, rooms sold excluding comp/house
- *Performance Statistics* — four occupancy variants (in/excluding OOO ×
  in/excluding comp and house use)
- *Revenue Performance* — ADR and RevPAR, each in two variants
- **Revenue Statistics** — taxable and exempt Room Revenue, then named misc
  lines: `SUNDRY - FOOD 1..6`, `DAMAGE FEE`, `LATE CANCEL ROOM REVENUE`,
  `OTHER REVENUE`, `DIFFERENCE DROP`
- **Taxes** — taxable and exempted × city / state / county / sales
- **Payments** — `CASH`, `AMEX`, `DISCOVER`, `MASTER`, `VISA`,
  `BILL TO COMPANY`
- *Guest Statistics*, *Guest Performance*, *Today's Activity*, *Forecast*

The middle three are revenue, tax and settlement totals — the same three
buckets a night audit's financial section produces, at **day grain instead of
transaction-code grain**. So this one report yields both statistic facts
(via `statistics.yaml`, lenient promotion, unmapped labels stay staged) and a
day-grain financial row set (via a new `mapping/hotelkey.yaml`, whose "codes"
are these printed line labels) — **staged and mapped, not promoted**, per
D-OH22.6.

The four occupancy variants and two ADR/RevPAR variants must each map to a
distinct `metric_code` or be left stage-only. Collapsing them onto one
`OCCUPANCY_PCT` would silently pick one definition; the samples show all four
agreeing, which proves nothing, because the mock property has no comp, house-use
or out-of-order rooms to separate them.

**D-OH22.4 — every `hotelkey.yaml` row ships `confidence: LOW,
review_status: needs-review`.**

Same posture as `skytouch.yaml`, for a stronger reason: HotelKey line labels
are property-configurable (`SUNDRY - FOOD 1` through `6` are obviously local
names), the sample is mock, and no real export exists to calibrate against.
The coverage report is the surface that carries this to the operator; §2.1 of
the SOS cutover note is the rule for how a needs-review dictionary is read
beside a parity result.

`DIFFERENCE DROP` in particular must not be guessed. It is a cash-handling
variance line, and whether it is revenue, a contra, or an operational statistic
changes the statement. Stage it, map nothing, let the worklist ask.

**D-OH22.5 — settlement, payments and AR aging stage but do not post.**

`Settlement By Payment Type.xlsx` is the only transaction-grain artifact in the
set, and it is the natural input for the settlements side. It is still not
posted this slice: the OH-27 journal posts from `usali_financial_fact` through
`gl_posting`, and a settlement row without its matching revenue row would post
an unbalanced day. AR aging is a *balance* report, not a day's activity, and
belongs with the ledger-balance facts (`ledgers.yaml`, `kind: balance`), not the
financial facts.

Staging them now is still worth doing: it makes the format work reviewable
against real files when they arrive, and the AR aging is the input OH-29 (AR/AP)
will want.

**D-OH22.6 — HotelKey ships as a statistics-and-balances source that is
explicitly NOT SOS-backing.** (§3 option 2, decided 2026-09-20.)

What promotes: statistic facts from the statistics report (`statistics.yaml`,
lenient) and ledger-balance facts from AR aging (`ledgers.yaml`,
`kind: balance`). What stays staged: **every financial row** — the statistics
report's Revenue Statistics, Taxes and Payments sections, the settlement
transactions, and the payment-type totals. They are staged with
`hotelkey.yaml` applied so the coverage worklist carries the LOW/needs-review
dictionary to the operator, but they are not promoted to
`usali_financial_fact`. Nothing from HotelKey reaches `gl_posting`, the parity
tripwire, or the statement.

Why not promote-but-not-post: the SOS renders line rows from the facts and
totals from the journal. Day-grain facts with no journal entries behind them
are lines under totals that do not exist — the drift signal firing by design,
on every HotelKey property, every day. And posting them is option 3 under
another name.

How the statement says so: a **source capability, not a property flag**. The
source declares, beside its detection signatures, whether it backs the
operating statement. The SOS API resolves the property's `pms_source`, and for
a non-backing source returns a `source_notice` and no sections; the SOS page
renders the notice in place of sections. Occupancy, ADR and RevPAR pages are
unaffected, because statistic facts promote as they do for every source.

When the night audit pack arrives, becoming SOS-backing is an adapter and a
dictionary calibration, not a redesign; the staged financial rows are the
calibration material.

## 3. What is blocked, and on what

**The trx-code-grain financial path is blocked on the HotelKey night audit
pack.** The sender flagged it himself when he sent the samples — he hit an
issue pulling it and would retrieve it from the system — and it never followed.

This matters more than a missing file, because of what OH-27 just shipped.
The SOS runs on **Shape C**: the journal owns every total, classified by the
chart, while the line rows inside each section keep rendering from the facts at
trx-code grain. Totals and detail agreeing is the normal state; disagreeing is
the deliberate, visible drift signal.

A HotelKey source built only from the statistics report would post correct
totals with **no trx-code detail beneath them** — permanently. Not drift, but
indistinguishable from it at a glance, on a statement whose whole design intent
is that a disagreement means something is wrong. Three ways out, none of which
should be chosen without the pack in hand:

1. Wait for the night audit pack and build the source properly.
2. Ship HotelKey as a statistics-and-balances source that is explicitly *not*
   SOS-backing, and make the statement say so rather than render empty sections.
3. Let day-grain revenue lines stand in as pseudo trx codes — cheapest, and the
   one that would quietly corrupt the meaning of the drift signal. Recorded
   here to be rejected on the record, not rediscovered later.

**Decided 2026-09-20: option 2** — D-OH22.6 is the record. Option 1 is still
the path to a full source once the pack arrives; option 3 stays rejected.

**Vendor API access is also outstanding** — the 2026-09-02 request to
`hkintegrations@hotelkeyapp.com` is unanswered. It does not block this slice:
everything above is file ingestion, which is how all four existing sources
work. It blocks a *pull* integration, which is not what OH-22 promises.

## 4. Architecture

No new stage. The slice adds, in dependency order:

1. `adaptors/xlsx.py` — workbook reader yielding the normalized stream
   (D-OH22.2), plus content-based format dispatch in `ingestion.process_file`.
2. `detect` discriminator — signature entries gain an optional header-window
   anchor; the `HOTEL STATISTICS` pair becomes unambiguous (D-OH22.1).
   **Shipped standalone ahead of the slice** (§8 decision 3, PR #135), with
   SkyTouch regression pins: plan
   [`2026-09-20-hotel-statistics-detect-collision.md`](../plans/2026-09-20-hotel-statistics-detect-collision.md).
   What remains for this slice is the `HOTELKEY` row itself, anchored on its
   `Report Run Date:` / `Report Run Time:` stamp.
3. `adaptors/hotelkey_hotel_statistics.py` — sectioned PDF parser (D-OH22.3).
4. `adaptors/hotelkey_settlement.py`, `..._all_payments.py`,
   `..._ar_aging.py` — XLSX parsers (D-OH22.5).
5. `mapping/hotelkey.yaml` — financial dictionary, all LOW/needs-review;
   additions to `statistics.yaml` and `ledgers.yaml` (D-OH22.4).
6. Source capability + SOS notice: the source declares `sos_backing=False`;
   the SOS API returns a `source_notice` for a property on such a source; the
   SOS page renders it (D-OH22.6).
7. Registry + signup: `HOTELKEY` in the detection registry, reaching the signup
   dropdown through `supported_pms_sources()` — **last**. §3 is resolved by
   D-OH22.6, so the dropdown advertises a source that ingests statistics and
   balances, and the SOS says so on the first visit.

## 5. Testing

The honest ceiling is format fidelity, and the tests should say so in their
names. What can be pinned:

- Every parser against a **committed synthetic fixture** in the sample's exact
  shape — never the sample itself.
- The detection collision, in **both directions**: a HotelKey statistics PDF
  resolves to `HOTELKEY`, a SkyTouch one still resolves to `SKYTOUCH`, and the
  anonymous `detect_report_signature` path is pinned for both. A test that only
  checks the new source passes with the discriminator deleted.
- The four occupancy variants map to four codes — asserted on a fixture where
  they **differ**, since the mock has them equal and an equal-valued fixture
  certifies nothing (the vacuous-test failure mode this repo has hit twice:
  see the night-audit `or True` assertion and the SchedulePage demand leak).
- `DIFFERENCE DROP` and every unmapped label stay staged and appear in the
  coverage worklist — mapping-by-accident fails the test.
- XLSX dispatch on magic bytes, not suffix; a `.xlsx` that is not a zip is
  refused loudly.

What cannot be pinned, and must not be claimed: that any HotelKey number is
correctly classified. No real export exists. The first real pack should arrive
as a calibration task with its own review, exactly as the choiceADVANTAGE
statistics recalibration did after its mock proved misleading.

## 6. Out of scope

API/pull integration (no credentials, and not what this slice promises);
posting HotelKey settlements to the journal (D-OH22.5); AR aging as anything
but staged balances (OH-29 owns AR); any `usali_financial_fact` row at
trx-code grain (§3).

## 7. Roadmap deltas

Applied by the plan's last task, not before, and to BOTH files (they carry the
status separately; flipping the yaml alone is a quarter of the job):

- `.github/roadmap.yml`: OH-22 → `in-progress`.
- `docs/ROADMAP.md` Tier 0 #1: the Why column gains "File ingestion shipped
  as a statistics-and-balances source (D-OH22.6); not SOS-backing until the
  night audit pack arrives; API/event stream pending vendor access." The
  entry's promise of an API + event stream is NOT met by this slice (§6) and
  the row must say so.
- Tier 0 ordering: unchanged pending §8 decision 2.

## 8. Open decisions — review outcome (2026-09-20)

1. **§3's three-way choice — DECIDED: ship as a non-SOS-backing source.**
   Recorded as D-OH22.6. Gates nothing further.
2. **Does OH-22 still lead Tier 0? — OPEN, and no longer blocking.** As of
   2026-09-02 the pilot property was choosing between HotelKey and Opera
   Cloud (Opera is already supported). The question is whether Tier 0's
   ranking should wait on that choice. Default until it is answered: the
   ranking stands and the slice proceeds, because the sample set is in hand
   and the work is file ingestion the next HotelKey property needs regardless
   of the pilot. If the pilot picks Opera Cloud, re-rank at the next roadmap
   pass; do not stop the slice.
3. **Should the detect fix ship on its own? — DECIDED: yes.** Built and
   reviewed as PR #135 ahead of the slice; the design's D-OH22.1 and
   architecture step 2 record what shipped and what remains.
