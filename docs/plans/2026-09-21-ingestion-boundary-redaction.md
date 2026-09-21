# Ingestion-boundary redaction (Tier 0 #2) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** no authenticated ingest path persists raw uploaded bytes; what is filed is a redacted words artifact (success) or an error record (failure).

**Architecture:** `redaction.redact_words` masks PANs across row tokens; `retention.py` holds the per-report policy table and writes the artifact / error record; `ingestion.py` gains a bytes core that the path wrappers, both API endpoints and the CLI call. Design: [`2026-09-21-ingestion-boundary-redaction-design.md`](../design/2026-09-21-ingestion-boundary-redaction-design.md).

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, pytest (`db_session`, `founding_org`, the night-audit app fixture in `tests/test_night_audit.py`; Docker for testcontainers).

**Conventions for every task:** TDD; American English; comments name the enforcement point or the test, never a cross-module guarantee; run `uv run --frozen ruff check` and `uv run --frozen mypy --strict src` before each commit; commit messages end with the session's attribution trailer.

---

### Task 1: `redact_words` — PAN masking across row tokens

**Files:**
- Modify: `src/usali/redaction.py`
- Test: `tests/test_redaction.py`

- [ ] **Step 1: Failing tests** (append to `tests/test_redaction.py`; it already imports from `usali.redaction`):

```python
from usali.adaptors.pdf import Word
from usali.redaction import redact_words


def _row(*texts: str, top: float = 10.0) -> list[Word]:
    return [Word(text=t, x0=20.0 * i, top=top) for i, t in enumerate(texts)]


def test_redact_words_masks_a_pan_split_across_four_words():
    words = _row("Card", "4111", "1111", "1111", "1111", "12.50")
    out, stats = redact_words(words)
    assert [w.text for w in out] == ["Card", "••••", "••••", "••••", "•••• 1111", "12.50"]
    assert stats.pans_masked == 1


def test_redact_words_masks_a_single_token_pan_and_keeps_last4():
    out, stats = redact_words(_row("Visa", "4111111111111111"))
    assert [w.text for w in out] == ["Visa", "•••• 1111"]
    assert stats.pans_masked == 1


def test_redact_words_leaves_non_luhn_digit_runs_alone():
    # A 16-digit confirmation number that fails Luhn is not a card.
    out, stats = redact_words(_row("Conf", "1234567890123456"))
    assert [w.text for w in out] == ["Conf", "1234567890123456"]
    assert stats.pans_masked == 0


def test_redact_words_scans_rows_not_the_whole_page():
    # Two rows whose digits would form a Luhn-valid run only if joined
    # across the row boundary must not be masked.
    words = _row("A", "4111 1111", top=10.0) + _row("1111 1111", "B", top=40.0)
    out, stats = redact_words(words)
    assert [w.text for w in out] == ["A", "4111 1111", "1111 1111", "B"]
    assert stats.pans_masked == 0


def test_redact_words_preserves_positions_and_order():
    words = _row("Total", "Occupied", "Rooms", "62")
    out, _ = redact_words(words)
    assert [(w.x0, w.top) for w in out] == [(w.x0, w.top) for w in words]
    assert [w.text for w in out] == ["Total", "Occupied", "Rooms", "62"]
```

- [ ] **Step 2: Run** `uv run --frozen pytest tests/test_redaction.py -q -p no:cacheprovider` → ImportError on `redact_words`.

- [ ] **Step 3: Implement** in `src/usali/redaction.py`:

```python
from dataclasses import dataclass, replace

from usali.adaptors.pdf import Word, cluster_rows


@dataclass(frozen=True)
class RedactionStats:
    pans_masked: int = 0
    cells_dropped: int = 0
    header_cells_dropped: int = 0


def redact_words(words: list[Word]) -> tuple[list[Word], RedactionStats]:
    """Mask Luhn-valid 13-19 digit runs in the retained words, row by row.

    A card can be printed as several tokens (``4111 1111 1111 1111``), so the
    scan runs over each clustered row's text, not over single words; a run
    that would only form across two rows is not a card
    (tests/test_redaction.py::test_redact_words_scans_rows_not_the_whole_page).
    Every covered word becomes ``••••``; the last keeps the final four
    digits. Output order and positions equal the input's.
    """
    masked: dict[int, str] = {}
    index_of = {id(w): i for i, w in enumerate(words)}
    pans = 0
    for row in cluster_rows(words):
        cells = sorted(row, key=lambda w: w.x0)
        spans: list[tuple[int, int, int]] = []  # (start, end, word index)
        text_parts: list[str] = []
        pos = 0
        for w in cells:
            if text_parts:
                pos += 1
            spans.append((pos, pos + len(w.text), index_of[id(w)]))
            text_parts.append(w.text)
            pos += len(w.text)
        text = " ".join(text_parts)
        for m in _PAN_RUN.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if not (13 <= len(digits) <= 19 and _luhn_ok(digits)):
                continue
            covered = [i for s, e, i in spans if s < m.end() and e > m.start()]
            if not covered:
                continue
            pans += 1
            for i in covered:
                masked[i] = "••••"
            masked[covered[-1]] = f"•••• {digits[-4:]}"
    out = [replace(w, text=masked[i]) if i in masked else w for i, w in enumerate(words)]
    return out, RedactionStats(pans_masked=pans)
```

`Word` is a dataclass (check `adaptors/pdf.py`; if it is not frozen/dataclass-replaceable, construct `Word(text=..., x0=w.x0, top=w.top)` instead). Keep `mask_pans`, `mask_names`, `redact` unchanged.

- [ ] **Step 4: Run** the module → all pass; ruff; mypy.
- [ ] **Step 5: Commit** `feat(redaction): redact_words masks Luhn-valid PANs across row tokens`.

### Task 2: `retention.py` — policy table, artifact, error record

> Superseded in review (2026-09-21): the shipped `_apply_columns` is per section, filters the header row too, always keeps column A, and never raises for a missing kept column; the snippet below is the first cut. Design D5.2 is the record.

**Files:**
- Create: `src/usali/retention.py`
- Create: `tests/test_retention.py`

- [ ] **Step 1: Failing tests** (`tests/test_retention.py`):

```python
"""The retention policy decides what a filed artifact keeps. Design D3-D6 in
docs/design/2026-09-21-ingestion-boundary-redaction-design.md."""

import json
from datetime import date
from pathlib import Path

from usali.adaptors.pdf import Word
from usali.adaptors.reader import read_words
from usali.ingestion import _PIPELINES
from usali.retention import (
    RETENTION,
    RetainedSection,
    build_artifact,
    write_artifact,
    write_error_record,
)

SETTLEMENT = Path("tests/fixtures/hotelkey/Settlement By Payment Type.xlsx")


def test_every_pipeline_has_a_retention_policy():
    # Closed set: a report type cannot ship without saying what it retains.
    assert set(RETENTION) == set(_PIPELINES)


def _section(words: list[Word], source: str, report_type: str) -> RetainedSection:
    return RetainedSection(
        title="t", pms_source=source, report_type=report_type,
        property_id="HKDEMO", business_date=date(2026, 8, 13), words=words,
    )


def test_settlement_artifact_drops_identity_columns_and_the_user_cell():
    words = read_words(SETTLEMENT)
    art = build_artifact(
        source_file="s.xlsx", data=SETTLEMENT.read_bytes(), kind="single",
        sections=[_section(words, "HOTELKEY", "settlement")], sections_dropped=[],
    )
    texts = {w["text"] for s in art["sections"] for w in s["words"]}
    header = {w.text for w in words}
    for dropped in ("Guest Name", "First Name", "Last Name", "Account Name", "Username", "Remarks"):
        assert dropped in header  # the fixture really has the column
        assert dropped not in texts
    assert not any(t.startswith("User:") for t in texts)
    assert "Folio Number" in texts and "Amount" in texts
    assert art["redaction"]["cells_dropped"] > 0
    assert art["redaction"]["header_cells_dropped"] == 1


def test_pdf_policy_keeps_all_words_after_pan_masking():
    words = [Word(text=t, x0=20.0 * i, top=10.0) for i, t in enumerate(
        ["Room", "Revenue", "4111", "1111", "1111", "1111"])]
    art = build_artifact(
        source_file="j.pdf", data=b"%PDF-", kind="single",
        sections=[_section(words, "SKYTOUCH", "hotel_journal")], sections_dropped=["A/R Aging"],
    )
    got = [w["text"] for w in art["sections"][0]["words"]]
    assert got == ["Room", "Revenue", "••••", "••••", "••••", "•••• 1111"]
    assert art["redaction"]["pans_masked"] == 1
    assert art["sections_dropped"] == ["A/R Aging"]
    assert art["sha256"] and art["bytes"] == 5 and art["kind"] == "single"


def test_write_artifact_names_by_stem_and_hash(tmp_path):
    art = build_artifact(source_file="pack.pdf", data=b"%PDF-x", kind="pack",
                         sections=[], sections_dropped=[])
    path = write_artifact(tmp_path / "done", art)
    assert path.name == f"pack.{art['sha256'][:8]}.redacted.json"
    assert json.loads(path.read_text())["source_file"] == "pack.pdf"


def test_write_error_record_holds_no_content(tmp_path):
    path = write_error_record(tmp_path / "fail", source_file="bad.pdf", data=b"%PDF-secret",
                              error="could not detect report type")
    rec = json.loads(path.read_text())
    assert set(rec) == {"source_file", "sha256", "bytes", "received_at", "error"}
    assert "secret" not in path.read_text()
    assert path.name.endswith(".error.json")
```

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement** `src/usali/retention.py`:

```python
"""What a filed ingest artifact keeps. Design D3-D6:
docs/design/2026-09-21-ingestion-boundary-redaction-design.md.

RETENTION is keyed exactly like ingestion._PIPELINES and pinned to it by
tests/test_retention.py::test_every_pipeline_has_a_retention_policy.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from usali.adaptors.pdf import Word
from usali.adaptors.xlsx import XLSX_COL_STEP, XLSX_ROW_STEP
from usali.redaction import RedactionStats, redact_words


@dataclass(frozen=True)
class Policy:
    keep_columns: tuple[str, ...] | None = None  # XLSX allowlist; None = keep every word


KEEP_ALL = Policy()

RETENTION: dict[tuple[str, str], Policy] = {
    ("OPERA", "trial_balance"): KEEP_ALL,
    ("OPERA", "manager_flash"): KEEP_ALL,
    ("OPERA", "market_stats"): KEEP_ALL,
    ("AUTOCLERK", "transaction_summary"): KEEP_ALL,
    ("AUTOCLERK", "manager_report"): KEEP_ALL,
    ("AUTOCLERK", "rate_plan"): KEEP_ALL,
    ("SKYTOUCH", "hotel_journal"): KEEP_ALL,
    ("SKYTOUCH", "hotel_statistics"): KEEP_ALL,
    ("HOTELKEY", "hotel_statistics"): KEEP_ALL,
    ("HOTELKEY", "settlement"): Policy(keep_columns=(
        "Account Category", "Date", "Time", "Transaction Number", "Folio Number",
        "Room Number", "Payment Type", "Payment Description", "Amount",
    )),
    ("HOTELKEY", "all_payments"): Policy(keep_columns=("Payment Type", "Amount")),
    # Every column: the label column's account names are what the adapter
    # stages (design §1, last paragraph); the AR slice owns that decision.
    ("HOTELKEY", "ar_aging"): Policy(keep_columns=None),
}

_XLSX_HEADER_DROP_PREFIXES = ("User:",)


@dataclass
class RetainedSection:
    title: str
    pms_source: str
    report_type: str
    property_id: str
    business_date: date
    words: list[Word] = field(default_factory=list)


def _apply_columns(words: list[Word], keep: tuple[str, ...]) -> tuple[list[Word], int]:
    """XLSX allowlist: find the header row that names every kept column; below
    it keep only those columns' cells; above it keep the title block."""
    by_row: dict[int, list[Word]] = {}
    for w in words:
        by_row.setdefault(int(round(w.top / XLSX_ROW_STEP)), []).append(w)
    header_row = None
    kept_cols: set[int] = set()
    for r in sorted(by_row):
        names = {w.text: int(round(w.x0 / XLSX_COL_STEP)) for w in by_row[r]}
        if all(k in names for k in keep):
            header_row = r
            kept_cols = {names[k] for k in keep}
            break
    if header_row is None:
        raise ValueError(f"retention: no header row names every kept column {keep}")
    out: list[Word] = []
    dropped = 0
    for r in sorted(by_row):
        for w in by_row[r]:
            if r <= header_row or int(round(w.x0 / XLSX_COL_STEP)) in kept_cols:
                out.append(w)
            else:
                dropped += 1
    return out, dropped


def _drop_header_cells(words: list[Word]) -> tuple[list[Word], int]:
    kept = [w for w in words if not w.text.startswith(_XLSX_HEADER_DROP_PREFIXES)]
    return kept, len(words) - len(kept)


def retain_section(section: RetainedSection) -> tuple[RetainedSection, RedactionStats]:
    policy = RETENTION[(section.pms_source, section.report_type)]
    words = section.words
    cells_dropped = header_dropped = 0
    if policy.keep_columns is not None:
        words, cells_dropped = _apply_columns(words, policy.keep_columns)
    if (section.pms_source, section.report_type) in _XLSX_REPORTS:
        words, header_dropped = _drop_header_cells(words)
    words, stats = redact_words(words)
    return (
        RetainedSection(section.title, section.pms_source, section.report_type,
                        section.property_id, section.business_date, words),
        RedactionStats(pans_masked=stats.pans_masked, cells_dropped=cells_dropped,
                       header_cells_dropped=header_dropped),
    )


_XLSX_REPORTS = frozenset({
    ("HOTELKEY", "settlement"), ("HOTELKEY", "all_payments"), ("HOTELKEY", "ar_aging"),
})


def build_artifact(*, source_file: str, data: bytes, kind: str,
                   sections: list[RetainedSection], sections_dropped: list[str]) -> dict[str, object]:
    kept: list[dict[str, object]] = []
    totals = RedactionStats()
    for s in sections:
        r, st = retain_section(s)
        totals = RedactionStats(
            pans_masked=totals.pans_masked + st.pans_masked,
            cells_dropped=totals.cells_dropped + st.cells_dropped,
            header_cells_dropped=totals.header_cells_dropped + st.header_cells_dropped,
        )
        kept.append({
            "title": r.title, "pms_source": r.pms_source, "report_type": r.report_type,
            "property_id": r.property_id, "business_date": r.business_date.isoformat(),
            "words": [{"text": w.text, "x0": w.x0, "top": w.top} for w in r.words],
        })
    return {
        "source_file": source_file,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "received_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "sections_dropped": list(sections_dropped),
        "redaction": {"pans_masked": totals.pans_masked, "cells_dropped": totals.cells_dropped,
                      "header_cells_dropped": totals.header_cells_dropped},
        "sections": kept,
    }


def _stem(source_file: str) -> str:
    return Path(source_file).stem or "upload"


def write_artifact(processed_dir: Path, artifact: dict[str, object]) -> Path:
    processed_dir.mkdir(parents=True, exist_ok=True)
    path = processed_dir / f"{_stem(str(artifact['source_file']))}.{str(artifact['sha256'])[:8]}.redacted.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False))
    return path


def write_error_record(failed_dir: Path, *, source_file: str, data: bytes, error: str) -> Path:
    failed_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    path = failed_dir / f"{_stem(source_file)}.{sha[:8]}.error.json"
    path.write_text(json.dumps({
        "source_file": source_file, "sha256": sha, "bytes": len(data),
        "received_at": datetime.now(UTC).isoformat(timespec="seconds"), "error": error[:500],
    }))
    return path
```

`XLSX_COL_STEP` / `XLSX_ROW_STEP` are the constants `adaptors/xlsx.py` uses to place cells (line ~80); import them by their real names. The settlement fixture's header row must name every kept column (it does: Task-time header dump 2026-09-21 lists all nine).

- [ ] **Step 4: Run** the module; ruff; mypy. The settlement fixture test may reveal the header row also contains cells the allowlist does not name (fine) or that `User:` sits in a merged cell with the value (`User: Sample TESTUSER` is one cell in the fixture; the prefix match handles it).
- [ ] **Step 5: Commit** `feat(retention): per-report retention policy, redacted artifact, error record`.

### Task 3: the bytes core in `ingestion.py`; filing becomes the artifact

**Files:**
- Modify: `src/usali/ingestion.py`, `src/usali/adaptors/pdf.py` (add `extract_pages_from_bytes`)
- Modify tests: `tests/test_ingestion.py`, `tests/test_ingestion_end_to_end.py`, `tests/test_skytouch_end_to_end.py`, `tests/test_hotelkey_end_to_end.py`, `tests/test_ledger_capture.py`, `tests/test_process_document.py`
- Create: `tests/test_ingestion_boundary.py`

- [ ] **Step 1: Failing test** (`tests/test_ingestion_boundary.py`):

```python
"""The gate's own pin (design §5): nothing under the ingest directories
carries the uploaded bytes after processing, on success or failure."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from usali.ingestion import ProcessingError, process_document_bytes, process_file
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules

SAMPLES = Path("docs/reference/samples")
PACK = SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf"
FLASH = SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf"


def _seed(db_session, *dicts):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    for d in dicts:
        load_mappings(db_session, f"mapping/{d}.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()


def _assert_no_file_carries(root: Path, data: bytes) -> None:
    sha = hashlib.sha256(data).hexdigest()
    probe = data[len(data) // 2: len(data) // 2 + 16]
    for f in root.rglob("*"):
        if f.is_file():
            blob = f.read_bytes()
            assert hashlib.sha256(blob).hexdigest() != sha, f
            assert probe not in blob, f


def test_success_files_a_redacted_artifact_and_no_raw_bytes(db_session, tmp_path):
    _seed(db_session, "skytouch")
    data = PACK.read_bytes()
    results = process_document_bytes(
        db_session, data, PACK.name, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
    )
    assert len(results) == 2
    art = json.loads(results[0].destination.read_text())
    assert art["sha256"] == hashlib.sha256(data).hexdigest()
    assert {s["report_type"] for s in art["sections"]} == {"hotel_journal", "hotel_statistics"}
    assert set(art["sections_dropped"]) == {"A/R Aging", "Cancellation List"}
    _assert_no_file_carries(tmp_path, data)


def test_failure_files_an_error_record_and_no_raw_bytes(db_session, tmp_path, founding_org):
    # No properties seeded: detection cannot resolve the property.
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    db_session.commit()
    data = FLASH.read_bytes()
    with pytest.raises(ProcessingError):
        process_document_bytes(
            db_session, data, FLASH.name, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
        )
    records = list((tmp_path / "fail").glob("*.error.json"))
    assert len(records) == 1 and "words" not in json.loads(records[0].read_text())
    assert not (tmp_path / "done").exists() or not any((tmp_path / "done").iterdir())
    _assert_no_file_carries(tmp_path, data)


def test_process_file_leaves_the_callers_file_in_place(db_session, tmp_path):
    _seed(db_session, "opera")
    src = tmp_path / "in" / FLASH.name
    src.parent.mkdir()
    shutil.copy(FLASH, src)
    r = process_file(db_session, src, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail")
    assert src.exists()  # the caller owns its file; nothing moves it
    assert r.destination.name.endswith(".redacted.json")
```

- [ ] **Step 2: Run** → ImportError on `process_document_bytes`.

- [ ] **Step 3: Implement.** In `adaptors/pdf.py` add `extract_pages_from_bytes(data: bytes) -> list[list[Word]]` beside `extract_pages`, sharing its per-page body (open `pdfplumber.open(io.BytesIO(data))`); make `extract_pages(path)` call it. In `ingestion.py`:

  1. `_file_hash(path)` → `_hash(data: bytes) -> str`.
  2. `_record_failure(session, name: str, data: bytes, exc)`: `source_file=name`, `file_hash=_hash(data)`.
  3. Delete `_move`.
  4. `process_bytes(session, data, name, *, processed_dir, failed_dir, edition=12) -> ProcessResult`: the body of today's `process_file` with `words = read_words_from_bytes(data)`, `_process_section(session, words, det, Path(name), _hash(data), edition)` (handlers use `path.name` only; passing `Path(name)` keeps their signature), commit, then `art = build_artifact(source_file=name, data=data, kind="single", sections=[RetainedSection(title=name, pms_source=det.pms_source, report_type=det.report_type, property_id=det.property_id, business_date=result.business_date, words=words)], sections_dropped=[])`, `dest = write_artifact(processed_dir, art)`, return `dataclasses.replace(result, destination=dest)`. On any exception: rollback, `_record_failure`, `write_error_record(failed_dir, source_file=name, data=data, error=str(exc))`, raise `ProcessingError(f"{name}: {exc}")`.
  5. `process_pack_bytes(...)`: today's `process_pack` body over `split_pack(extract_pages_from_bytes(data))`, collecting `RetainedSection`s for recognized sections and titles for skipped ones; one artifact with `kind="pack"`; every result's `destination` is the artifact path. Failure path as in 4.
  6. `process_document_bytes(...)`: today's routing over `data` (no re-read); `is_pdf(data)`; the probe on `read_words_from_bytes(data)`.
  7. `process_file`, `process_pack`, `process_document`: `data = Path(path).read_bytes()` then the bytes function with `name=Path(path).name`. No move, no delete.
  8. Keep the `process_document` docstring's claims; update `process_file` / `process_pack` docstrings: "files a redacted artifact (retention.py) to processed_dir; on failure records an error record to failed_dir; never moves or deletes `path`".

- [ ] **Step 4: Update the existing filing assertions** (each `exists()` on `<name>` becomes the artifact or error record):
  - `tests/test_ingestion.py:41,121,137` → `assert list(processed.glob("*.redacted.json"))` (one per processed file; assert the count the test expects). `:79` → `assert list(failed.glob("*.error.json"))`. `:111` → `assert not (failed.exists() and any(failed.glob("*.error.json")))`.
  - `tests/test_ingestion_end_to_end.py:44` → two `*.redacted.json`.
  - `tests/test_skytouch_end_to_end.py:48,71,72,118,119` → artifact / error-record globs; also `r.destination == artifact path` for every result (`len({r.destination for r in results}) == 1`).
  - `tests/test_hotelkey_end_to_end.py:196`, `tests/test_ledger_capture.py:288` → error-record glob.
  - `tests/test_process_document.py`: the five `exists()` lines → artifact / error-record globs; keep every other assertion.
  Read each test's intent before editing; the assertion changes name, not meaning.

- [ ] **Step 5: Run** the touched modules; then ruff, mypy, then the full suite (`uv run --frozen pytest -q -p no:cacheprovider`, ~13 min). Report the tail.
- [ ] **Step 6: Commit** `feat(ingestion): process from bytes; file a redacted artifact, never the upload`.

### Task 4: the API endpoints stop writing the inbox

**Files:**
- Modify: `src/usali/server.py` (`/ingest`), `src/usali/night_audit_api.py` (`upload_night_audit_report`, `_ingest_pack`)
- Modify tests: `tests/test_server.py`, `tests/test_night_audit.py`; extend `tests/test_ingestion_boundary.py`

- [ ] **Step 1: Failing tests.** In `tests/test_ingestion_boundary.py` add an API-level test per endpoint using the fixtures those test modules already use (read `tests/test_server.py` for the `/ingest` client fixture and `tests/test_night_audit.py:20-40` for the night-audit app fixture with `inbox_dir`, `processed_dir`, `failed_dir`): upload the Opera flash (single) and the SkyTouch pack (night-audit, property STDEMO) and assert `_assert_no_file_carries(<the app's ingest root>, payload)` plus an artifact exists. Also in `tests/test_night_audit.py` add `test_pack_validation_recognizes_a_section_by_its_title`: a pack whose one recognized section prints a `Rate Plan` column heading in its first 120 words (build words like the fixture in `tests/test_detect_signature.py::test_section_title_decides_identity_not_a_body_column_heading`) is accepted, not skipped; it fails before the `_ingest_pack` change because `detect(section.words, registry)` has no title.

- [ ] **Step 2: Implement.** `/ingest`: drop the inbox `mkdir`/`xb` block and the 409; call `process_bytes(session, payload, upload_name, processed_dir=processed, failed_dir=failed)`. Night-audit upload: drop the `dest` write and the `_refuse` unlink (keep `_refuse` returning the `HTTPException`); the single path validates on `read_words_from_bytes(payload)` and calls `process_bytes`; `_ingest_pack` takes `payload: bytes` and `upload_name`, splits `extract_pages_from_bytes(payload)`, calls `detect(section.words, registry, section.title)`, and hands the bytes to `process_pack_bytes`. `app.state.ingest_dirs` stays a triple (the inbox is still `usali watch`'s directory).

- [ ] **Step 3: Run** `tests/test_server.py tests/test_night_audit.py tests/test_ingestion_boundary.py`; ruff; mypy.
- [ ] **Step 4: Commit** `feat(api): uploads never touch the inbox; pack validation detects by section title`.

### Task 5: the CLI

**Files:**
- Modify: `src/usali/cli.py` (`process_cmd`, `watch_cmd`)
- Modify: `tests/test_cli_commands.py`

- [ ] **Step 1: Failing tests.** `test_watch_deletes_the_inbox_file_after_processing` and `test_watch_deletes_the_inbox_file_after_a_failure` (stub `process_file` like `test_watch_drains_every_accepted_suffix_already_in_the_inbox` does, once returning a result and once raising `ProcessingError`; assert the inbox is empty afterwards). `test_process_leaves_the_argument_in_place` for `usali process`.
- [ ] **Step 2: Implement.** `watch_cmd.handle`: after `process_file` returns or raises `ProcessingError`, `path.unlink(missing_ok=True)`. Help text: "processed files are deleted from the inbox; the redacted artifact or error record is the trace". `process_cmd`: unchanged behavior, help text says the argument is left in place.
- [ ] **Step 3: Run**, ruff, mypy. **Step 4: Commit** `feat(cli): watch consumes the inbox file; process leaves its argument`.

### Task 6: docs, roadmap, full gates, PR

- [ ] **Step 1:** `docs/ROADMAP.md`: §2 bullet "Ingestion-boundary redaction is preview-only" → "shipped 2026-09-21 (OH-32): uploads are processed in memory and only a redacted words artifact is filed"; Tier 0 row 2 Id `—` → `OH-32 (shipped)`; open decision 6 → the settled paragraph ("destructive; raw bytes never persist; design 2026-09-21"); §7 gains a dated deltas note. `.github/roadmap.yml`: add `OH-32 Ingestion-boundary redaction`, status `shipped  # PR #<n>, 2026-09-21`, summary in user-facing terms ("A report you upload is read in memory; the only copy kept is a redacted extract of the figures the product uses, never the original file, so guest names and card numbers from the rest of the pack are not stored"). OH-24's summary: "Every recognized report is kept as a redacted extract, searchable by business date and report, with a record of who reviewed it and when." `docs/ARCHITECTURE.md`: one sentence under the ingestion section naming `retention.py`.
- [ ] **Step 2:** manual verification, never committed: `uv run --frozen python` the real pack through `process_pack_bytes` against a testcontainers session is heavy; instead run `build_artifact` on `split_pack(extract_pages(<real pack>))` filtered to recognized sections and print: words retained, sections dropped count, `pans_masked`, and a Luhn scan of the artifact text (expect 0). Paste the numbers in the PR body.
- [ ] **Step 3:** `uv run --frozen ruff check && uv run --frozen mypy --strict src && uv run --frozen pytest -q -p no:cacheprovider`; frontend untouched.
- [ ] **Step 4:** commit `docs(roadmap): ingestion-boundary redaction shipped as OH-32`, push, open the PR with the design decisions, the measured table, the manual verification numbers, and the AR aging out-of-scope note.
