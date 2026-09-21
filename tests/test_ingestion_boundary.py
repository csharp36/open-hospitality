"""The gate's own pin (design section 5): nothing under the ingest directories
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
    assert len({r.destination for r in results}) == 1
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
    # The only copy of the flash under the ingest dirs is the source the caller placed.
    _assert_no_file_carries(tmp_path / "done", FLASH.read_bytes())
