# HotelKey + SkyTouch PMS Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two new PMS sources to the ingestion pipeline — **HotelKey** (file-based bridge, statistics only for now) and **SkyTouch** (bundled night-audit pack: financial + statistics) — reusing the existing detect → parse → stage → promote architecture.

**Architecture:** Each PMS report becomes a per-report adapter emitting the existing `StagedRecord`/`StatisticRecord` schemas, registered via one line in `detect._REPORT_SIGNATURES` + one handler in `ingestion._PIPELINES`. Two genuinely new pieces: (1) a **pack splitter** so one SkyTouch PDF containing ~12 report sections is split into per-report word-lists before dispatch; (2) synthetic **mock fixtures** for SkyTouch because the real sample contains live guest PII.

**Tech Stack:** Python 3.13, pdfplumber (PDF word extraction), pydantic v2 (record schemas), SQLAlchemy (stage/fact tables), pytest. No new runtime dependency required for Part 2; Part 1 (HotelKey) uses the existing `pdfplumber` for the statistics PDF (Excel adapters are deferred — see Scope).

---

## Scope & sequencing

Two independent subsystems in one plan (they share the pipeline). Each produces working, testable software on its own; they may be executed in either order.

- **Part 1 — HotelKey file-based bridge (build now).** Samples are mock ("Sample DEVUSER") so they are safe fixtures. Deliverable: the **Hotel Statistics** PDF adapter → `UsaliStatisticFact` (occupancy/ADR/RevPAR + revenue-by-category + taxes). **Out of scope / deferred:** the HotelKey Excel files (All Payments, AR Aging, Settlement By Payment Type) — they are reconciliation/settlement data, one carries guest names, and none is the coded revenue feed; and HotelKey's **USALI revenue feed**, which requires the "Final Audit Report" sample we do not have. These are tracked in "Deferred work" at the end.

- **Part 2 — SkyTouch (build after user approval).** Deliverable: the pack splitter + the **Hotel Journal Summary** financial adapter → `UsaliFinancialFact` and the **Hotel Statistics** adapter → `UsaliStatisticFact`, driven by a `mapping/skytouch.yaml` code dictionary seeded from the **Final Transaction Closeout** code universe. **Out of scope / deferred:** SkyTouch **segmentation** (`Revenue by Market` / `Revenue by Rate Code`) — available in choiceADVANTAGE but not enabled in the sample property's pack; it becomes buildable once a property enables it (onboarding step).

**Hard rule (Part 2):** the real SkyTouch sample at `~/Desktop/Sample Hotel/Skytouch/…` contains **real guest names and card numbers**. It must NEVER be committed to the repo or used as a test fixture. Task S1 generates a fully synthetic fixture; every later SkyTouch task depends on it.

---

## File structure

**Part 1 — HotelKey (create):**
- `src/usali/adaptors/hotelkey_hotel_statistics.py` — `extract_business_date`, `parse_hotel_statistics(words, …) -> list[StatisticRecord]`.
- `tests/adaptors/test_hotelkey_hotel_statistics.py` — unit test over a JSON word-fixture.
- `tests/fixtures/hotelkey_hotel_statistics_words.json` — extracted words from the mock sample PDF.
- `docs/reference/samples/HotelKey - Hotel Statistics.pdf` — the mock sample (safe; DEVUSER) for the e2e test.

**Part 1 — HotelKey (modify):**
- `src/usali/normalize.py` — add `parse_hotelkey_date` (`"Aug 13, 2026"`).
- `src/usali/detect.py` — add `("HOTEL STATISTICS", ("HOTELKEY", "hotel_statistics"))` to `_REPORT_SIGNATURES` **(ordering caveat — see Task H4)**.
- `src/usali/ingestion.py` — add `_run_hotelkey_hotel_statistics` + register in `_PIPELINES`.
- `src/usali/performance.py` — add `"hotel_statistics"` to `REQUIRED_STATISTICS_REPORTS`.
- `mapping/statistics.yaml` — add `{source: HOTELKEY, label, code}` rows.
- `mapping/properties.yaml` — add the HotelKey demo property alias.

**Part 2 — SkyTouch (create):**
- `src/usali/adaptors/pack.py` — `split_pack(pages: list[list[Word]]) -> list[ReportSection]` pack splitter.
- `src/usali/adaptors/skytouch_hotel_journal.py` — `parse_hotel_journal(words, …) -> list[StagedRecord]`.
- `src/usali/adaptors/skytouch_hotel_statistics.py` — `parse_hotel_statistics(words, …) -> list[StatisticRecord]`.
- `mapping/skytouch.yaml` — SkyTouch transaction-code → USALI dictionary.
- `tests/adaptors/test_skytouch_hotel_journal.py`, `test_skytouch_hotel_statistics.py`, `tests/adaptors/test_pack.py`.
- `tests/fixtures/skytouch_hotel_journal_words.json`, `skytouch_hotel_statistics_words.json` — SYNTHETIC.
- `scripts/gen_skytouch_mock_fixtures.py` — the one-time synthetic-fixture generator (Task S1).
- `docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf` — SYNTHETIC mock pack for the e2e test.

**Part 2 — SkyTouch (modify):**
- `src/usali/adaptors/pdf.py` — add `extract_pages(pdf_path) -> list[list[Word]]` (per-page words).
- `src/usali/normalize.py` — add `parse_skytouch_date` (`"6/21/2026"`) and `parse_paren_amount` (`"(918.29)"` → negative).
- `src/usali/detect.py` — add SkyTouch signatures.
- `src/usali/ingestion.py` — add SkyTouch handlers; teach `process_file` to split a pack into sections and process each (returns a list of `ProcessResult`).
- `mapping/statistics.yaml`, `mapping/properties.yaml` — SkyTouch rows.

---

# PART 1 — HotelKey file-based bridge

### Task H1: HotelKey date parser

**Files:**
- Modify: `src/usali/normalize.py`
- Test: `tests/test_normalize.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_normalize.py  (add)
from datetime import date
from usali.normalize import parse_hotelkey_date

def test_parse_hotelkey_date():
    assert parse_hotelkey_date("Aug 13, 2026") == date(2026, 8, 13)
    assert parse_hotelkey_date("Aug 13 2026") == date(2026, 8, 13)  # some rows drop the comma
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_normalize.py::test_parse_hotelkey_date -v`
Expected: FAIL — `ImportError: cannot import name 'parse_hotelkey_date'`.

- [ ] **Step 3: Implement**

```python
# src/usali/normalize.py  (add)
from datetime import datetime

def parse_hotelkey_date(text: str) -> date:
    return datetime.strptime(text.replace(",", "").strip(), "%b %d %Y").date()
```

- [ ] **Step 4: Run it, verify it passes.** Run: `uv run pytest tests/test_normalize.py::test_parse_hotelkey_date -v`

- [ ] **Step 5: Commit** — `git commit -m "feat(hotelkey): add HotelKey date parser"`

---

### Task H2: capture the HotelKey Hotel Statistics word-fixture

The mock sample is at `~/Desktop/Sample Hotel/HotelKey/Hotel Statistics - HK.pdf` (safe — DEVUSER mock data).

- [ ] **Step 1: Copy the sample into the repo** (it is mock, so committing is allowed)

```bash
cp "$HOME/Desktop/Sample Hotel/HotelKey/Hotel Statistics - HK.pdf" \
   "docs/reference/samples/HotelKey - Hotel Statistics.pdf"
```

- [ ] **Step 2: Generate the word-fixture JSON**

```bash
uv run python -c "
import json
from usali.adaptors.pdf import extract_words
ws = extract_words('docs/reference/samples/HotelKey - Hotel Statistics.pdf')
json.dump([{'text': w.text, 'x0': w.x0, 'top': w.top} for w in ws],
          open('tests/fixtures/hotelkey_hotel_statistics_words.json','w'), indent=0)
print(len(ws), 'words')
"
```

- [ ] **Step 3: Eyeball the fixture** — confirm it contains the header row `Description Actual Today M-T-D LY-M-T-D Y-T-D LY-T-D` and rows like `Total Rooms 60 780 0 3,540 0`. Commit both files.

`git commit -m "test(hotelkey): add mock Hotel Statistics sample + word fixture"`

---

### Task H3: the Hotel Statistics adapter

The report has five value columns anchored by the header row `Actual Today | M-T-D | LY-M-T-D | Y-T-D | LY-T-D` (the first token `Actual`/`Today` wraps across two words in extraction — anchor on the five numeric columns). Metric label is the far-left text; sections are `Room Statistics`, `Performance Statistics`, `Revenue Statistics`, `Guest Statistics`, `Today's Activity`, etc. Model each value as a `StatisticRecord` with `period_label` ∈ {`ACTUAL`,`MTD`,`LY_MTD`,`YTD`,`LY_YTD`} and `is_prior_year = period_label in {"LY_MTD","LY_YTD"}`.

**Files:**
- Create: `src/usali/adaptors/hotelkey_hotel_statistics.py`
- Test: `tests/adaptors/test_hotelkey_hotel_statistics.py`

- [ ] **Step 1: Write the failing test** (values below are read directly from the mock sample — page 1 `Total Rooms 60 780 0 3,540 0`, page 1 `Room Sold 26 363 0 1,604 0`, page 2 `Taxable Room Revenue 3,664.59 …`)

```python
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from usali.adaptors.hotelkey_hotel_statistics import parse_hotel_statistics
from usali.adaptors.pdf import Word

def _words():
    d = json.loads(Path("tests/fixtures/hotelkey_hotel_statistics_words.json").read_text())
    return [Word(text=x["text"], x0=x["x0"], top=x["top"]) for x in d]

def test_parses_room_and_revenue_metrics():
    recs = parse_hotel_statistics(_words(), property_id="HKDEMO", business_date=date(2026, 8, 13))
    by = {(r.metric_label, r.period_label): r.value for r in recs}
    assert by[("Total Rooms", "ACTUAL")] == Decimal("60")
    assert by[("Room Sold", "ACTUAL")] == Decimal("26")
    assert by[("Room Sold", "YTD")] == Decimal("1604")
    assert by[("Taxable Room Revenue", "ACTUAL")] == Decimal("3664.59")

def test_prior_year_flag_and_source():
    recs = parse_hotel_statistics(_words(), property_id="HKDEMO", business_date=date(2026, 8, 13))
    assert recs and all(r.pms_source == "HOTELKEY" and r.report_type == "hotel_statistics" for r in recs)
    ly = [r for r in recs if r.period_label in ("LY_MTD", "LY_YTD")]
    assert ly and all(r.is_prior_year for r in ly)
```

- [ ] **Step 2: Run it, verify it fails** (module missing). Run: `uv run pytest tests/adaptors/test_hotelkey_hotel_statistics.py -v`

- [ ] **Step 3: Implement the adapter** (model on `opera_manager_flash.py`'s anchor+nearest-column approach; five columns instead of six)

```python
import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.schemas import StatisticRecord

_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")
_PERIODS = ["ACTUAL", "MTD", "LY_MTD", "YTD", "LY_YTD"]  # left-to-right column order
_HEADER_TOKENS = ["Today", "M-T-D", "LY-M-T-D", "Y-T-D", "LY-T-D"]  # 5 column anchors


def extract_business_date(words: list[Word]) -> date:
    # HotelKey prints "Date: Aug 13, 2026" in the header block.
    from usali.normalize import parse_hotelkey_date
    toks = [w.text for w in words[:60]]
    for i, t in enumerate(toks):
        if t == "Date:" and i + 3 < len(toks):
            return parse_hotelkey_date(" ".join(toks[i + 1 : i + 4]))
    raise ValueError("no HotelKey 'Date: Mon DD, YYYY' header found")


def _anchors(rows: list[list[Word]]) -> list[float]:
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        texts = [w.text for w in cells]
        # the five period tokens appear consecutively; find them and take their x0
        for j in range(len(texts) - 4):
            if texts[j : j + 5] == _HEADER_TOKENS:
                return [cells[j + k].x0 for k in range(5)]
    raise ValueError("HotelKey statistics column header not found")


def parse_hotel_statistics(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StatisticRecord]:
    rows = cluster_rows(words, y_tol)
    anchors = _anchors(rows)
    label_boundary = anchors[0] - 20
    out: list[StatisticRecord] = []
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        label = " ".join(w.text for w in cells if w.x0 < label_boundary).strip()
        values = [w for w in cells if w.x0 >= label_boundary and _NUM_RE.match(w.text)]
        if not label or len(values) == 0:
            continue
        for w in values:
            idx = min(range(5), key=lambda i: abs(anchors[i] - w.x0))
            period = _PERIODS[idx]
            out.append(StatisticRecord(
                property_id=property_id, pms_source="HOTELKEY", report_type="hotel_statistics",
                business_date=business_date, metric_label=label, period_label=period,
                is_prior_year=period in ("LY_MTD", "LY_YTD"),
                value=Decimal(w.text.replace(",", "")),
            ))
    return out
```

- [ ] **Step 4: Run it, verify it passes.** Run: `uv run pytest tests/adaptors/test_hotelkey_hotel_statistics.py -v`. If a metric mismatches, adjust `label_boundary`/anchor detection against the fixture — do not change the asserted values.

- [ ] **Step 5: Commit** — `git commit -m "feat(hotelkey): parse the Hotel Statistics report"`

---

### Task H4: register detection (with the ordering caveat)

**CAVEAT:** SkyTouch also has a report literally titled `Hotel Statistics`. Detection matches the first signature whose phrase appears in the header. HotelKey's header contains the property name + "Hotel Statistics"; SkyTouch's contains "Property Name: … Hotel Statistics". A bare `"HOTEL STATISTICS"` phrase would collide. Disambiguate on a source-unique token: HotelKey headers carry `"Report Run Date"`; SkyTouch carries `"Business Date"` / `"Property Code"`. Use a **more specific** signature phrase for each.

**Files:** Modify `src/usali/detect.py`; Test `tests/test_detect.py`

- [ ] **Step 1: Write the failing detection test**

```python
# tests/test_detect.py  (add)
import json
from pathlib import Path
from usali.adaptors.pdf import Word
from usali.detect import detect

_HK_REGISTRY = [{"match": "SUMMIT LODGE", "property_id": "HKDEMO", "pms_source": "HOTELKEY"}]

def _hk_words():
    d = json.loads(Path("tests/fixtures/hotelkey_hotel_statistics_words.json").read_text())
    return [Word(text=x["text"], x0=x["x0"], top=x["top"]) for x in d]

def test_detects_hotelkey_hotel_statistics():
    det = detect(_hk_words(), _HK_REGISTRY)
    assert (det.pms_source, det.report_type) == ("HOTELKEY", "hotel_statistics")
```

(Registry `match` phrase = a property-name token present in the fixture header, e.g. `SUMMIT LODGE` for the `Summit Lodge Redstone, TX` mock property. Confirm the exact token in the fixture and use it.)

- [ ] **Step 2: Run it, verify it fails** ("could not detect report type"). Run: `uv run pytest tests/test_detect.py::test_detects_hotelkey_hotel_statistics -v`

- [ ] **Step 3: Implement** — add a HotelKey-specific signature ABOVE any future generic `HOTEL STATISTICS`:

```python
# src/usali/detect.py  — add to _REPORT_SIGNATURES
    ("REPORT RUN DATE", ("HOTELKEY", "hotel_statistics")),  # HotelKey-only header token
```

Note: `REPORT RUN DATE` appears on every HotelKey report; for now HotelKey has only the statistics report so this is unambiguous. When the HotelKey financial (Final Audit Report) lands, tighten this to a per-report phrase — recorded in Deferred work.

- [ ] **Step 4: Run it, verify it passes.** Run: `uv run pytest tests/test_detect.py::test_detects_hotelkey_hotel_statistics -v`

- [ ] **Step 5: Commit** — `git commit -m "feat(hotelkey): detect Hotel Statistics reports"`

---

### Task H5: statistics mapping + property seed

**Files:** Modify `mapping/statistics.yaml`, `mapping/properties.yaml`

- [ ] **Step 1:** Read the promoted metric codes already used by Opera/Autoclerk in `mapping/statistics.yaml`. Add `{source: HOTELKEY, label: "<HotelKey metric label>", code: "<existing USALI metric code>"}` rows for the metrics we surface: `Occupancy …`, `ADR`, `RevPAR`, `Total Rooms`, `Rooms Available To Sell`, `Room Sold`, `Taxable Room Revenue`, `Total Room Revenue`, `Arrivals`, `Departures`, `No Shows`. Map each HotelKey label to the SAME `code` the Opera/Autoclerk equivalents use so downstream performance queries are source-agnostic.
- [ ] **Step 2:** Add the HotelKey demo property to `mapping/properties.yaml`: `{match: "SUMMIT LODGE", property_id: "HKDEMO", pms_source: "HOTELKEY", name: "Summit Lodge (HotelKey demo)"}` (match token confirmed against the fixture header).
- [ ] **Step 3:** `promote_statistics` is lenient (unmapped labels are skipped, not fatal), so partial mapping is safe. Commit.

`git commit -m "feat(hotelkey): map Hotel Statistics metrics + seed demo property"`

---

### Task H6: wire the ingestion handler

**Files:** Modify `src/usali/ingestion.py`, `src/usali/performance.py`; Test `tests/test_hotelkey_end_to_end.py`

- [ ] **Step 1: Write the failing e2e test** (model on `tests/test_statistics_end_to_end.py`; seed schedules + properties, drop the mock PDF through `process_file`)

```python
from datetime import date
from pathlib import Path
import shutil
from usali.ingestion import process_file
# ... db_session, seed_schedules, seed_properties fixtures as in the existing e2e tests ...

def test_hotelkey_statistics_end_to_end(db_session, tmp_path):
    src = Path("docs/reference/samples/HotelKey - Hotel Statistics.pdf")
    drop = tmp_path / src.name
    shutil.copy(src, drop)
    result = process_file(db_session, drop, processed_dir=tmp_path/"done", failed_dir=tmp_path/"fail")
    assert result.pms_source == "HOTELKEY"
    assert result.report_type == "hotel_statistics"
    assert result.property_id == "HKDEMO"
    assert result.staged > 0
```

- [ ] **Step 2: Run it, verify it fails** — `KeyError: ('HOTELKEY', 'hotel_statistics')` in `_PIPELINES`.

- [ ] **Step 3: Implement the handler** (model exactly on `_run_opera_manager_flash`)

```python
# src/usali/ingestion.py
from usali.adaptors import hotelkey_hotel_statistics as hk_stats

def _run_hotelkey_hotel_statistics(session, words, det, path, file_hash, edition):
    business_date = hk_stats.extract_business_date(words)
    records = hk_stats.parse_hotel_statistics(words, property_id=det.property_id, business_date=business_date)
    batch = stage_statistics(session, records, source_file=path.name, file_hash=file_hash)
    r = promote_statistics(session, "mapping/statistics.yaml", source=det.pms_source, business_date=business_date)
    return batch, business_date, _Counts(len(records), r.promoted, 0, r.skipped)

# register:
_PIPELINES[("HOTELKEY", "hotel_statistics")] = _run_hotelkey_hotel_statistics
```

Also add `"hotel_statistics"` to `REQUIRED_STATISTICS_REPORTS` in `src/usali/performance.py`.

- [ ] **Step 4: Run it, verify it passes.** Run: `uv run pytest tests/test_hotelkey_end_to_end.py -v`

- [ ] **Step 5: Full suite + commit** — `uv run pytest -q` then `git commit -m "feat(hotelkey): wire Hotel Statistics into the ingestion pipeline"`

**Part 1 done:** HotelKey statistics ingest end-to-end. Revenue feed + Excel reports deferred (see end).

---

# PART 2 — SkyTouch (build after user approval)

### Task S1: generate synthetic mock fixtures (PII gate — do first)

The real pack has live PII and must not enter the repo. Produce synthetic word-fixtures that reproduce the *structure* (headers, column positions, transaction codes, paren-negative amounts) with invented names/figures, following the demo's fictitious-by-construction posture.

**Files:** Create `scripts/gen_skytouch_mock_fixtures.py`, `tests/fixtures/skytouch_hotel_journal_words.json`, `tests/fixtures/skytouch_hotel_statistics_words.json`, `docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf`

- [ ] **Step 1:** Write `scripts/gen_skytouch_mock_fixtures.py` that emits, as `Word` JSON, a synthetic **Hotel Journal Summary** section with these EXACT invented rows (used verbatim by Task S4's test) — header `Description (Transaction Code) Postings Corrections Adjustments Totals Guest Ledger AR Ledger AdvDep Ledger` then:
  - `Cash (CA) (500.00) 0.00 0.00 (500.00) (500.00) 0.00 0.00`
  - `Room Charge (RM) 4000.00 0.00 0.00 4000.00 4000.00 0.00 0.00`
  - `State Tax (T1) 250.00 0.00 0.00 250.00 250.00 0.00 0.00`
  - `Visa Payment (VI) (3750.00) 0.00 0.00 (3750.00) (3750.00) 0.00 0.00`
  - `Today's Total: 0.00 0.00 0.00 0.00 0.00 0.00 0.00`

  and a synthetic **Hotel Statistics** section. **All figures FULLY INVENTED** (real-sample figures must never appear — fictitious-by-construction rule). Column-anchor header row uses five CLEAN single-word tokens at the five column x0s: `PTD PTD1 LYPTD YTD LYYTD` (do NOT use multi-word "Last Year PTD"). Data rows (ACTUAL / PTD / LY_PTD / YTD / LY_YTD):
  - `Total Rooms 100 2100 2100 12600 12600`
  - `Total Occupied Rooms 62 1300 1250 7800 7600`
  - `ADR for Total Occupied Rooms 88.50 90.10 92.25 91.40 93.75`
  - `RevPar 54.87 55.75 57.10 56.30 58.20`
  - `Total Room Revenue 5487.00 117130.00 115312.50 442520.00 435800.00`
  - ACTUAL column ties by construction: ADR 88.50 × 62 occupied = 5487.00 room revenue; 5487.00 / 100 rooms = 54.87 RevPar.

  Assign realistic `x0`/`top` values (label at x0≈75, five/eight numeric columns at increasing x0; use the real column x0 positions read from the live sample as a template but with the synthetic text above). Business-date header line: `Property Name: Redstone Test Inn` / `Business Date: 6/21/2026 Property Code: TEST1`.

- [ ] **Step 2:** Also build a small synthetic multi-page mock **pack PDF** (3–4 pages: an A/R Aging filler page, the Hotel Journal Summary page, the Hotel Statistics page) via `reportlab` (add as a dev dependency: `uv add --dev reportlab`) — each page's first line is the report title, matching the real pack's structure. Save to `docs/reference/samples/`.
- [ ] **Step 3:** Run the generator; commit the script, the two JSON fixtures, and the mock pack PDF. **Verify zero real names/numbers** (grep the fixtures for any token from the real sample — must be absent).

`git commit -m "test(skytouch): synthetic mock fixtures (no real PII)"`

---

### Task S2: SkyTouch amount + date normalizers

SkyTouch renders negatives in parentheses `(918.29)` and dates as `M/D/YYYY`.

**Files:** Modify `src/usali/normalize.py`; Test `tests/test_normalize.py`

- [ ] **Step 1: Failing test**

```python
from datetime import date
from decimal import Decimal
from usali.normalize import parse_paren_amount, parse_skytouch_date

def test_parse_paren_amount():
    assert parse_paren_amount("(918.29)") == Decimal("-918.29")
    assert parse_paren_amount("4,003.21") == Decimal("4003.21")
    assert parse_paren_amount("0.00") == Decimal("0.00")

def test_parse_skytouch_date():
    assert parse_skytouch_date("6/21/2026") == date(2026, 6, 21)
```

- [ ] **Step 2: Run, verify fail.** Run: `uv run pytest tests/test_normalize.py -k "paren or skytouch" -v`

- [ ] **Step 3: Implement**

```python
# src/usali/normalize.py
def parse_paren_amount(text: str) -> Decimal:
    t = text.strip().replace(",", "")
    if t.startswith("(") and t.endswith(")"):
        return -Decimal(t[1:-1])
    return Decimal(t)

def parse_skytouch_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%m/%d/%Y").date()
```

- [ ] **Step 4: Run, verify pass. Step 5: Commit** — `git commit -m "feat(skytouch): paren-negative amount + M/D/YYYY date parsers"`

---

### Task S3: per-page extraction primitive

**Files:** Modify `src/usali/adaptors/pdf.py`; Test `tests/adaptors/test_pdf.py`

- [ ] **Step 1: Failing test** — a 2-page fixture PDF (reuse the mock pack from S1) yields a list-of-pages, each a `list[Word]`, page 0 words all have smaller `top` than page 1's isn't required, but each page's words are grouped separately.

```python
from usali.adaptors.pdf import extract_pages
def test_extract_pages_groups_by_page():
    pages = extract_pages("docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf")
    assert len(pages) >= 3
    assert all(isinstance(p, list) and p for p in pages)
    # first line of each page is its report title
    assert pages[0][0].text  # non-empty
```

- [ ] **Step 2: Run, verify fail** (no `extract_pages`).

- [ ] **Step 3: Implement** (mirror `extract_words` but keep the page boundary)

```python
def extract_pages(pdf_path: str | Path) -> list[list[Word]]:
    pages: list[list[Word]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append([Word(text=w["text"], x0=float(w["x0"]), top=float(w["top"]))
                          for w in page.extract_words()])
    return pages
```

- [ ] **Step 4: Run, verify pass. Step 5: Commit** — `git commit -m "feat(ingest): per-page word extraction for report packs"`

---

### Task S4: the pack splitter

Group consecutive pages sharing the same report-title header into one `ReportSection`. The title is each page's first clustered row (top-most, left-most text). Re-offset each section's words so multi-page sections don't overlap vertically (the adapters rely on monotonic `top`).

**Files:** Create `src/usali/adaptors/pack.py`; Test `tests/adaptors/test_pack.py`

- [ ] **Step 1: Failing test** (constructed synthetic pages — no PDF needed, so the splitter is unit-testable)

```python
from usali.adaptors.pdf import Word
from usali.adaptors.pack import split_pack

def _page(title, *rows):
    ws = [Word(text=t, x0=75.0, top=10.0) for t in title.split()]
    for i, row in enumerate(rows, start=1):
        ws += [Word(text=t, x0=75.0 + 60*j, top=10.0 + 15*i) for j, t in enumerate(row.split())]
    return ws

def test_split_groups_consecutive_same_title_pages():
    pages = [
        _page("A/R Aging", "acct 1 2"),
        _page("Guest Ledger", "g 1"),
        _page("Guest Ledger", "g 2"),          # same title -> same section
        _page("Hotel Statistics", "Total Rooms 5"),
    ]
    sections = split_pack(pages)
    assert [s.title for s in sections] == ["A/R Aging", "Guest Ledger", "Hotel Statistics"]
    # the 2-page Guest Ledger section merged both pages' words
    gl = next(s for s in sections if s.title == "Guest Ledger")
    assert any(w.text == "g" for w in gl.words)
    tops = [w.top for w in gl.words]
    assert len(set(tops)) > 1  # re-offset kept rows distinct
```

- [ ] **Step 2: Run, verify fail** (no module).

- [ ] **Step 3: Implement**

```python
from dataclasses import dataclass
from usali.adaptors.pdf import Word, cluster_rows

@dataclass(frozen=True)
class ReportSection:
    title: str
    words: list[Word]

_PAGE_GAP = 2000.0  # vertical offset between merged pages; exceeds any real page height

def _page_title(page: list[Word]) -> str:
    rows = cluster_rows(page)
    if not rows:
        return ""
    top_row = min(rows, key=lambda r: min(w.top for w in r))
    return " ".join(w.text for w in sorted(top_row, key=lambda w: w.x0)).strip()

def split_pack(pages: list[list[Word]]) -> list[ReportSection]:
    sections: list[ReportSection] = []
    for page in pages:
        title = _page_title(page)
        if sections and sections[-1].title == title:
            base = max(w.top for w in sections[-1].words) + _PAGE_GAP
            sections[-1].words.extend(
                Word(text=w.text, x0=w.x0, top=w.top + base) for w in page
            )
        else:
            sections.append(ReportSection(title=title, words=list(page)))
    return sections
```

(Note: `ReportSection.words` is mutated via `.extend`; make it a plain `list` field — drop `frozen` or use `field(default_factory=list)`. Adjust the dataclass so the test's mutation path works.)

- [ ] **Step 4: Run, verify pass. Step 5: Commit** — `git commit -m "feat(ingest): split a night-audit pack into per-report sections"`

---

### Task S5: the Hotel Journal Summary financial adapter

Structure (real, from the sample): header `Description (Transaction Code) Postings Corrections Adjustments Totals …`; each data row is `<name> (<CODE>) <postings> <corrections> <adjustments> <totals> …`. The transaction code is the parenthesised token at the end of the description. Emit one `StagedRecord` per coded row using the **Postings** column as `raw_amount` (the day's posting), paren-negative aware. Reconcile against the `Today's Total:` row.

**Files:** Create `src/usali/adaptors/skytouch_hotel_journal.py`; Test `tests/adaptors/test_skytouch_hotel_journal.py`

- [ ] **Step 1: Failing test** (asserts the synthetic values from Task S1)

```python
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from usali.adaptors.skytouch_hotel_journal import parse_hotel_journal
from usali.adaptors.pdf import Word

def _words():
    d = json.loads(Path("tests/fixtures/skytouch_hotel_journal_words.json").read_text())
    return [Word(text=x["text"], x0=x["x0"], top=x["top"]) for x in d]

def test_parses_codes_and_paren_negatives():
    recs = parse_hotel_journal(_words(), property_id="STDEMO", business_date=date(2026, 6, 21))
    by = {r.pms_trx_code: r for r in recs}
    assert by["RM"].raw_amount == Decimal("4000.00")
    assert by["CA"].raw_amount == Decimal("-500.00")
    assert by["VI"].raw_amount == Decimal("-3750.00")
    assert by["T1"].raw_amount == Decimal("250.00")
    assert "Today's Total" not in {r.pms_trx_desc for r in recs}
    assert all(r.pms_source == "SKYTOUCH" and r.report_type == "hotel_journal" for r in recs)

def test_reconciles_against_todays_total():
    # postings sum to 0.00 in the fixture; a mutated fixture that doesn't tie must raise
    recs = parse_hotel_journal(_words(), property_id="STDEMO", business_date=date(2026, 6, 21))
    assert sum(r.raw_amount for r in recs) == Decimal("0.00")
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement** (model on `opera_trial_balance.parse_trial_balance`; code regex `\(([A-Z0-9]{1,6})\)$` on the description; Postings = first amount column after the code)

```python
import re
from datetime import date
from decimal import Decimal
from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_paren_amount, parse_skytouch_date
from usali.schemas import StagedRecord

_CODE_RE = re.compile(r"^\(([A-Z0-9]{1,6})\)$")
_AMOUNT_RE = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?$")
_TOTAL_LABEL = "Today's Total:"

def extract_business_date(words: list[Word]) -> date:
    toks = [w.text for w in words[:80]]
    for i, t in enumerate(toks):
        if t == "Date:" or t == "Business" and toks[i+1:i+2] == ["Date:"]:
            pass
    for t in toks:
        if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", t):
            return parse_skytouch_date(t)
    raise ValueError("no SkyTouch M/D/YYYY business date found")

def parse_hotel_journal(words, *, property_id: str, business_date: date, y_tol: float = 3.0) -> list[StagedRecord]:
    out: list[StagedRecord] = []
    total_row_sum: Decimal | None = None
    for row in cluster_rows(words, y_tol):
        cells = sorted(row, key=lambda w: w.x0)
        toks = [w.text for w in cells]
        joined = " ".join(toks)
        amounts = [t for t in toks if _AMOUNT_RE.match(t)]
        if joined.startswith(_TOTAL_LABEL) and amounts:
            total_row_sum = parse_paren_amount(amounts[0])
            continue
        code_idx = next((i for i, t in enumerate(toks) if _CODE_RE.match(t)), None)
        if code_idx is None or not amounts:
            continue
        code = _CODE_RE.match(toks[code_idx]).group(1)
        desc = " ".join(toks[:code_idx]).strip() or None
        postings = parse_paren_amount(amounts[0])  # first amount column = Postings
        out.append(StagedRecord(
            property_id=property_id, pms_source="SKYTOUCH", report_type="hotel_journal",
            business_date=business_date, pms_trx_code=code, pms_trx_desc=desc, raw_amount=postings,
        ))
    if total_row_sum is not None and sum(r.raw_amount for r in out) != total_row_sum:
        raise ValueError(
            f"SkyTouch Hotel Journal postings {sum(r.raw_amount for r in out)} "
            f"!= Today's Total {total_row_sum} — layout changed?"
        )
    return out
```

- [ ] **Step 4: Run, verify pass** (fix `extract_business_date` cruft — keep only the digit-date scan). **Step 5: Commit** — `git commit -m "feat(skytouch): parse the Hotel Journal Summary financial feed"`

---

### Task S6: the SkyTouch Hotel Statistics adapter

Five value columns: `Business Date | PTD | Last Year PTD | YTD | Last YTD` → `period_label` ∈ {`ACTUAL`,`PTD`,`LY_PTD`,`YTD`,`LY_YTD`}, `is_prior_year = period in {"LY_PTD","LY_YTD"}`. Same anchor+nearest-column approach as Task H3.

**Files:** Create `src/usali/adaptors/skytouch_hotel_statistics.py`; Test `tests/adaptors/test_skytouch_hotel_statistics.py`

- [ ] **Step 1: Failing test** (asserts S1 synthetic values: `Total Rooms` ACTUAL Decimal("100"), `RevPar` ACTUAL Decimal("54.87"), `Total Room Revenue` YTD Decimal("442520.00"), and that `ADR for Total Occupied Rooms` ACTUAL == Decimal("88.50")).
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — copy Task H3's adapter, change `pms_source="SKYTOUCH"`, `report_type="hotel_statistics"`, `_PERIODS=["ACTUAL","PTD","LY_PTD","YTD","LY_YTD"]`, and anchor on the five clean header tokens `["PTD","PTD1","LYPTD","YTD","LYYTD"]` (the S1 fixture places these single-word tokens at the five column x0 positions). `is_prior_year = period in ("LY_PTD","LY_YTD")`.
- [ ] **Step 4: Run, verify pass. Step 5: Commit** — `git commit -m "feat(skytouch): parse the Hotel Statistics report"`

---

### Task S7: SkyTouch detection signatures

**Files:** Modify `src/usali/detect.py`; Test `tests/test_detect.py`

- [ ] **Step 1: Failing test** — a fabricated header word-list containing `HOTEL JOURNAL SUMMARY` detects `("SKYTOUCH","hotel_journal")`; one containing `Business Date … Hotel Statistics … Property Code` detects `("SKYTOUCH","hotel_statistics")` and does NOT collide with HotelKey's `REPORT RUN DATE` signature.
- [ ] **Step 2: Run, verify fail.**
- [ ] **Step 3: Implement** — add, ordered so specific phrases win:

```python
    ("HOTEL JOURNAL SUMMARY", ("SKYTOUCH", "hotel_journal")),
    ("PROPERTY CODE", ("SKYTOUCH", "hotel_statistics")),  # SkyTouch stats header; paired w/ split-pack context
```

**CAVEAT** — `detect()` runs on a single report's words. Under the pack flow (Task S8) each section is detected independently, so `PROPERTY CODE` only ever sees a SkyTouch stats section. But `HOTEL JOURNAL SUMMARY` must be listed and both must sit AFTER HotelKey's `REPORT RUN DATE`. If a real collision surfaces, switch the stats signature to the pack-title (`"HOTEL STATISTICS"` matched against the section title from `split_pack`, not the raw header) — see Task S8 which already carries the section title.

- [ ] **Step 4: Run, verify pass. Step 5: Commit** — `git commit -m "feat(skytouch): detection signatures"`

---

### Task S8: teach the pipeline to ingest a pack

`process_file` today = one report per file. A SkyTouch pack is many. Introduce section iteration: if the file splits into >1 titled section, process each section that maps to a known `(pms_source, report_type)` and skip the rest (housekeeping/vacant lists etc.); return a `list[ProcessResult]`. Single-report files keep working (one section → a one-element list). Use the section title from `split_pack` to disambiguate SkyTouch reports rather than re-detecting from raw header text.

**Files:** Modify `src/usali/ingestion.py`; Test `tests/test_skytouch_end_to_end.py`

- [ ] **Step 1: Failing e2e test** — drop the synthetic mock pack PDF (Task S1) through the new entry point; assert two `ProcessResult`s: `("SKYTOUCH","hotel_journal")` and `("SKYTOUCH","hotel_statistics")`, both `property_id="STDEMO"`, and that irrelevant sections (A/R Aging filler) are skipped.

```python
def test_skytouch_pack_end_to_end(db_session, tmp_path):
    src = Path("docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf")
    drop = tmp_path / src.name; shutil.copy(src, drop)
    results = process_pack(db_session, drop, processed_dir=tmp_path/"done", failed_dir=tmp_path/"fail")
    kinds = {(r.pms_source, r.report_type) for r in results}
    assert ("SKYTOUCH", "hotel_journal") in kinds
    assert ("SKYTOUCH", "hotel_statistics") in kinds
```

- [ ] **Step 2: Run, verify fail** (no `process_pack`).
- [ ] **Step 3: Implement** — a `SECTION_TITLES: dict[str, tuple[str,str]]` mapping split-pack titles (`"Hotel Journal Summary"`, `"Hotel Statistics"`, `"Final Transaction Closeout"`) to `(source, report_type)`; a new `process_pack(session, pdf_path, *, processed_dir, failed_dir, edition=12) -> list[ProcessResult]` that: `extract_pages` → `split_pack` → for each section whose title is in `SECTION_TITLES`, build a `Detection` (property from `load_registry` matched on the section words), run the existing `_PIPELINES` handler, `record_coverage`, and collect a `ProcessResult`; commit once at the end; on failure roll back + quarantine the whole file (one file = one batch-group). Register SkyTouch handlers `_run_skytouch_hotel_journal` (stage_records → transform, like `_run_opera_trial_balance` minus the ledger rider) and `_run_skytouch_hotel_statistics` (stage_statistics → promote_statistics) in `_PIPELINES`. Keep `process_file` delegating to `process_pack` and returning the single element for non-pack files, OR have the CLI/watcher call `process_pack`. Decide and document the entry point; update `cli.py`'s `process` command to use `process_pack`.
- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(skytouch): ingest the night-audit pack section-by-section"`

---

### Task S9: SkyTouch code dictionary + property seed

**Files:** Create `mapping/skytouch.yaml`; Modify `mapping/properties.yaml`, `mapping/statistics.yaml`

- [ ] **Step 1:** Build `mapping/skytouch.yaml` from the **Final Transaction Closeout** code universe (the sample lists the full set grouped by type: `RM` Room Charge, `NS` No Show, `EXCO` Extended Check-Out, `MR` Meeting Room, `T1` State Tax, `T2` City/County Tax, `CA` Cash, `CK` Check, `PO` Cash Paid Out, `AX`/`DS`/`MC`/`VI` cards, `DB`/`DR` direct bill, `EFT`/`FC`/`VDPEFT`/`VDPFEE` admin, `MS` Misc, `PET` Pet). One `{source: SKYTOUCH, code, edition: 12, schedule_id, major, sub, line_item, gl_account_code, confidence: LOW, review_status: needs-review, notes}` row per code, USALI-classified by analogy to `mapping/opera.yaml`. Mark every row `needs-review` (codes are property/franchise-configurable — the Opera-draft posture).
- [ ] **Step 2:** Seed: `uv run usali seed-mappings mapping/skytouch.yaml`. Add the SkyTouch demo property to `mapping/properties.yaml` (`{match: "REDSTONE TEST INN", property_id: "STDEMO", pms_source: "SKYTOUCH", name: "…"}`) and SkyTouch statistics-label rows to `mapping/statistics.yaml` (map `RevPar`/`ADR for Total Occupied Rooms`/`Total Occupied Rooms`/`Total Room Revenue` → the shared USALI metric codes).
- [ ] **Step 3:** Add a `transform`-level test that an unmapped SkyTouch code raises the loud `MappingException` path (existing behavior; just assert it holds for SKYTOUCH). Commit.

`git commit -m "feat(skytouch): transaction-code dictionary + demo property seed"`

---

### Task S10: full-suite green + docs

- [ ] **Step 1:** `uv run pytest -q` — entire suite green.
- [ ] **Step 2:** `uv run ruff check src tests && uv run mypy src` — clean (mypy is src-only per repo convention).
- [ ] **Step 3:** Update `README.md` ingestion section: supported PMS list now {Opera, AutoClerk, HotelKey, SkyTouch}; note SkyTouch = bundled pack, HotelKey = statistics-only bridge (revenue via API later). Note the onboarding dropdown must include all four (wire to the same registry the detector uses).
- [ ] **Step 4: Commit** — `git commit -m "docs: document HotelKey + SkyTouch ingestion"`

---

## Deferred work (tracked, not in this plan)

1. **HotelKey revenue feed** — needs a "Final Audit Report" sample (coded revenue/ledger). Until then HotelKey has no `UsaliFinancialFact`. Add to the owner ask.
2. **HotelKey → API adapter** — the strategic target (replace the file bridge). Blocked on API access (owner pursuing). Build as a Delphi/Gusto-style adapter when credentials land.
3. **HotelKey Excel reports** (All Payments, AR Aging, Settlement) — need an `adaptors/excel.py` (openpyxl) primitive; Settlement carries guest names (PII handling). Low value without the revenue feed.
4. **SkyTouch segmentation** — `Revenue by Market` / `Revenue by Rate Code` → `SegmentRecord`/`market_stats`-style adapter. Needs a property to enable the report in its audit pack (onboarding step), then a fresh (mock) sample.
5. **Detection hardening** — when HotelKey gains a second report type, tighten the `REPORT RUN DATE` signature to a per-report phrase.

---

## Self-review notes

- **Spec coverage:** SkyTouch financial (S5) + statistics (S6) + pack handling (S3/S4/S8) + mapping (S9); HotelKey statistics (H3) + wiring (H6). Segmentation + HotelKey revenue explicitly deferred with reasons. ✓
- **PII gate:** S1 precedes every SkyTouch adapter task; real sample never committed. ✓
- **Type consistency:** adapters emit `StagedRecord`/`StatisticRecord` (unchanged schemas); handlers match `_Handler` signature; `report_type` strings (`hotel_journal`, `hotel_statistics`) are identical across adapter emit, `_REPORT_SIGNATURES`/`SECTION_TITLES`, and `_PIPELINES`. ✓
- **Open risk to validate during execution:** the SkyTouch stats header wraps multi-word column labels ("Last Year PTD"); Task S6 anchors on the numeric columns as the fallback. Confirm against the mock fixture during S6.
