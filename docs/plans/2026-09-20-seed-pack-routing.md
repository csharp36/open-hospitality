# Seed pack routing (#138) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the demo seed and the Playwright backend ingest every committed sample, packs included, instead of dying at the SkyTouch pack.

**Architecture:** a new `process_document` in `src/usali/ingestion.py` tries single-report detection and falls back to `process_pack` only when that raises; both scripts call it and load the two missing dictionaries. Design: [`2026-09-20-seed-pack-routing-design.md`](../design/2026-09-20-seed-pack-routing-design.md).

**Tech Stack:** Python 3.13, SQLAlchemy, pytest with `db_session` / `founding_org` from `tests/conftest.py` (Postgres via testcontainers; Docker required).

---

### Task 1: `process_document` and its tests

**Files:**
- Modify: `src/usali/ingestion.py` (imports near the top; new function directly after `process_pack`)
- Create: `tests/test_process_document.py`

- [ ] **Step 1: Write the failing tests**

```python
"""process_document routes a file to the single-report path or the pack path.

Design: docs/design/2026-09-20-seed-pack-routing-design.md (D1). The pack
sample is the one committed file whose whole-file detection fails; every
single-report sample detects whole-file, and must never be split, because
split_pack carves a multi-page single report at page-title changes.
"""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

import usali.ingestion as ingestion
from usali.ingestion import ProcessingError, process_document
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import IngestBatch

SAMPLES = Path("docs/reference/samples")
PACK = SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf"
MANAGER_REPORT = SAMPLES / "Autoclerk - Manager Report 07.07.2026.pdf"
HOTELKEY_XLSX = Path("tests/fixtures/hotelkey/All Payments.xlsx")


def _seed(db_session, *dictionaries: str) -> None:
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    for d in dictionaries:
        load_mappings(db_session, f"mapping/{d}.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()


def _drop(tmp_path: Path, sample: Path) -> Path:
    target = tmp_path / "inbox" / sample.name
    target.parent.mkdir(exist_ok=True)
    shutil.copy(sample, target)
    return target


def _failed_batches(db_session) -> int:
    return db_session.scalar(
        select(func.count()).select_from(IngestBatch).where(IngestBatch.status == "failed")
    )


def _no_pack_path(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("process_pack must not be called for a single report")
    monkeypatch.setattr(ingestion, "process_pack", boom)


def test_pack_sample_routes_to_the_pack_path(db_session, tmp_path):
    _seed(db_session, "skytouch")
    results = process_document(
        db_session, _drop(tmp_path, PACK),
        processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
    )
    assert {(r.pms_source, r.report_type) for r in results} == {
        ("SKYTOUCH", "hotel_journal"), ("SKYTOUCH", "hotel_statistics"),
    }
    assert all(r.property_id == "STDEMO" for r in results)
    assert (tmp_path / "done" / PACK.name).exists()
    assert _failed_batches(db_session) == 0


def test_single_report_never_takes_the_pack_path(db_session, tmp_path, monkeypatch):
    # The manager report splits into four "sections" of which one resolves;
    # the pack path would keep 104 of its 336 words. It must not be offered
    # the choice.
    _seed(db_session, "autoclerk")
    _no_pack_path(monkeypatch)
    results = process_document(
        db_session, _drop(tmp_path, MANAGER_REPORT),
        processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
    )
    assert [(r.pms_source, r.report_type, r.property_id) for r in results] == [
        ("AUTOCLERK", "manager_report", "SSSJ")
    ]
    assert results[0].staged > 0
    assert (tmp_path / "done" / MANAGER_REPORT.name).exists()


def test_xlsx_never_takes_the_pack_path(db_session, tmp_path, monkeypatch):
    _seed(db_session, "hotelkey")
    _no_pack_path(monkeypatch)
    results = process_document(
        db_session, _drop(tmp_path, HOTELKEY_XLSX),
        processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
    )
    assert len(results) == 1 and results[0].pms_source == "HOTELKEY"
    assert (tmp_path / "done" / HOTELKEY_XLSX.name).exists()


def test_a_pdf_that_fails_both_ways_reports_both_reasons(db_session, tmp_path, founding_org):
    # Dictionaries but NO properties: whole-file detection cannot resolve the
    # property, and every pack section is skipped for the same reason.
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/skytouch.yaml")
    db_session.commit()
    with pytest.raises(ProcessingError) as excinfo:
        process_document(
            db_session, _drop(tmp_path, PACK),
            processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
        )
    msg = str(excinfo.value)
    assert "as a single report:" in msg and "as a pack:" in msg
    assert (tmp_path / "fail" / PACK.name).exists()
    assert not (tmp_path / "done" / PACK.name).exists()
    assert _failed_batches(db_session) == 1
```

- [ ] **Step 2: Run them; all four must fail at import**

Run: `uv run --frozen pytest tests/test_process_document.py -q -p no:cacheprovider`
Expected: `ImportError: cannot import name 'process_document'`.

- [ ] **Step 3: Implement**

In `src/usali/ingestion.py`, make sure these names are imported from `usali.adaptors.reader` (the module already imports `read_words` from somewhere; add the two names to that import or alongside it): `is_pdf`, `read_words_from_bytes`. Then add, directly after `process_pack`:

```python
def process_document(
    session: Session,
    path: str | Path,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> list[ProcessResult]:
    """Ingest one file whose shape is not known in advance: a single report
    (PDF or XLSX) or a bundled night-audit pack (PDF only).

    Single-report detection is tried first. `detect` raises when the header
    window's first matching signature disagrees with the property's registered
    source (its cross-check) or when nothing matches; a pack shows exactly
    that, because its first page is whichever report the PMS bound first. A
    file that detects as a single report is processed as one and never split:
    `split_pack` carves a multi-page single report at every page-title change,
    so the pack path would drop the pages whose titles resolve to nothing.
    Pinned in tests/test_process_document.py::test_single_report_never_takes_the_pack_path.

    Failure semantics are those of the path taken. When the pack path fails
    too, the raised ProcessingError names both reasons; the failed batch and
    the quarantine were already recorded by `process_pack`.
    """
    src = Path(path)
    data = src.read_bytes()
    if not is_pdf(data):
        return [process_file(session, src, processed_dir=processed_dir,
                             failed_dir=failed_dir, edition=edition)]
    try:
        detect(read_words_from_bytes(data), load_registry(session))
    except ValueError as single_exc:
        try:
            return process_pack(session, src, processed_dir=processed_dir,
                                failed_dir=failed_dir, edition=edition)
        except ProcessingError as pack_exc:
            raise ProcessingError(
                f"{src.name}: as a single report: {single_exc}; as a pack: {pack_exc}"
            ) from pack_exc
    return [process_file(session, src, processed_dir=processed_dir,
                         failed_dir=failed_dir, edition=edition)]
```

- [ ] **Step 4: Run the module, then ruff and mypy**

Run: `uv run --frozen pytest tests/test_process_document.py -q -p no:cacheprovider`
Expected: 4 passed.

Run: `uv run --frozen ruff check && uv run --frozen mypy --strict src`
Expected: `All checks passed!` and `Success: no issues found`.

- [ ] **Step 5: Commit**

```
feat(ingestion): process_document routes a file to the single-report or pack path

Single-report detection is tried first; only when it raises does the pack
path run. A section count cannot be the router: split_pack carves a
multi-page single report at page-title changes, and the AutoClerk manager
report would keep 104 of its 336 words. Pinned on the committed samples.
```

### Task 2: the two scripts use it, plus the seed's end-to-end pin

**Files:**
- Modify: `scripts/demo_seed.py` (`_seed_base`, `_seed_documents`, the `process_file` import at line 75)
- Modify: `scripts/e2e_backend.py` (the `load_mappings` block near line 119 and the sample loop near line 258)
- Create: `tests/test_demo_seed_documents.py`

- [ ] **Step 1: Write the failing test**

```python
"""The demo seed's document step ingests every committed sample, the pack
included (issue #138). Runs the real scripts/demo_seed.py functions against
the real docs/reference/samples directory."""

import hashlib
import importlib.util
from pathlib import Path

from sqlalchemy import func, select

from usali.models import IngestBatch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed_documents", _SCRIPT)
assert _spec is not None and _spec.loader is not None
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)

SAMPLES = Path("docs/reference/samples")
PACK = SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_seed_documents_ingests_every_sample_including_the_pack(db_session):
    demo_seed._seed_base(db_session)
    demo_seed._seed_documents(db_session)

    rows = db_session.execute(
        select(IngestBatch.file_hash, IngestBatch.status, IngestBatch.pms_source)
    ).all()
    assert rows and {status for _, status, _ in rows} == {"transformed"}
    assert {h for h, _, _ in rows} == {_sha(p) for p in SAMPLES.glob("*.pdf")}
    pack_rows = [src for h, _, src in rows if h == _sha(PACK)]
    assert pack_rows == ["SKYTOUCH", "SKYTOUCH"]  # journal + statistics sections

    before = db_session.scalar(select(func.count()).select_from(IngestBatch))
    demo_seed._seed_documents(db_session)  # per-file idempotent
    assert db_session.scalar(select(func.count()).select_from(IngestBatch)) == before
```

- [ ] **Step 2: Run it and watch it fail at the pack**

Run: `uv run --frozen pytest tests/test_demo_seed_documents.py -q -p no:cacheprovider`
Expected: FAIL with `ProcessingError` mentioning `SkyTouch - Standard Audit Pack (mock).pdf` and `registered for SKYTOUCH`. If it fails earlier (for example `_seed_base` raising), stop and report BLOCKED with the output.

- [ ] **Step 3: Change the seed**

In `scripts/demo_seed.py`:

1. Line 75: `from usali.ingestion import process_file, record_coverage` becomes `from usali.ingestion import process_document, record_coverage` (keep `record_coverage`; drop `process_file` only if nothing else in the file uses it, which `grep -n process_file scripts/demo_seed.py` will show).
2. `_seed_base`: after the `autoclerk.yaml` line add
   ```python
       load_mappings(session, str(REPO_ROOT / "mapping" / "skytouch.yaml"))
       load_mappings(session, str(REPO_ROOT / "mapping" / "hotelkey.yaml"))
   ```
3. `_seed_documents`: the call becomes
   ```python
               process_document(session, target, processed_dir=work / "processed",
                                failed_dir=work / "failed")
   ```
   and the docstring's first line becomes `"""Ingest the sample PDFs: single reports and the choiceADVANTAGE (SKYTOUCH) pack, every property."""`; the closing print becomes
   ```python
       print(f"  ingested {ingested} sample PDFs, {skipped} already present "
             "(Opera/AutoClerk 2026-07-07, choiceADVANTAGE (SKYTOUCH) pack 2026-06-21, "
             "HotelKey statistics 2026-08-13)")
   ```

- [ ] **Step 4: Run the seed test**

Run: `uv run --frozen pytest tests/test_demo_seed_documents.py -q -p no:cacheprovider`
Expected: 1 passed.

- [ ] **Step 5: Change the Playwright backend the same way**

In `scripts/e2e_backend.py`: after the `autoclerk.yaml` `load_mappings` line (near 120) add the same two `load_mappings` lines for `skytouch.yaml` and `hotelkey.yaml`; in the sample loop (near 258) replace `process_file(` with `process_document(`, and fix the import that brings `process_file` in (grep for it; import `process_document` instead, keeping any other imported names). No automated test drives this script. Then read `frontend/e2e/*.spec.ts` (or wherever `frontend/playwright.config.ts` points) and grep for numbers or names that the two extra samples could change: batch counts, property lists, "6", "six", "SSSJ", "HISJ", "STDEMO", "HKDEMO". Report what you find; do not edit specs.

- [ ] **Step 6: Gates**

Run: `uv run --frozen ruff check && uv run --frozen mypy --strict src && uv run --frozen pytest -q -p no:cacheprovider`
Expected: ruff clean, mypy `Success`, pytest all green (main: 2311 passed / 4 skipped; expect 2316 passed / 4 skipped).

- [ ] **Step 7: Commit**

```
fix(seed): route the sample packs through the pack path; load every sample's dictionary

The demo seed and the Playwright backend handed the SkyTouch-format pack to
process_file, whose header window matched AutoClerk rate_plan and tripped
the property source cross-check, so both stopped at that file. Both now
call process_document and load skytouch.yaml and hotelkey.yaml so the
pack's and HotelKey's rows map instead of landing in MappingException.
Pinned end to end against the real sample directory.

Closes #138.
```
