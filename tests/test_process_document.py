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
    # process_document routes through process_document_bytes, which calls
    # process_pack_bytes — patching the path wrapper would catch nothing.
    def boom(*_args, **_kwargs):
        raise AssertionError("the pack path must not be taken for a single report")
    monkeypatch.setattr(ingestion, "process_pack_bytes", boom)


def test_pack_sample_routes_to_the_pack_path(db_session, tmp_path):
    _seed(db_session, "skytouch")
    results = process_document(
        db_session, _drop(tmp_path, PACK),
        processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
    )
    assert len(results) == 2
    assert {(r.pms_source, r.report_type) for r in results} == {
        ("SKYTOUCH", "hotel_journal"), ("SKYTOUCH", "hotel_statistics"),
    }
    assert all(r.property_id == "STDEMO" for r in results)
    artifacts = list((tmp_path / "done").glob("*.redacted.json"))
    assert len(artifacts) == 1  # one artifact for the pack
    assert {r.destination for r in results} == {artifacts[0]}
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
    assert len(list((tmp_path / "done").glob("*.redacted.json"))) == 1


def test_xlsx_never_takes_the_pack_path(db_session, tmp_path, founding_org, monkeypatch):
    # No properties seeded, so single-report detection WOULD raise; an XLSX
    # must still never be offered the pack path (packs are PDF-only) and
    # fails through process_file like any single report.
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/hotelkey.yaml")
    db_session.commit()
    _no_pack_path(monkeypatch)
    with pytest.raises(ProcessingError) as excinfo:
        process_document(
            db_session, _drop(tmp_path, HOTELKEY_XLSX),
            processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
        )
    assert "as a pack:" not in str(excinfo.value)
    assert len(list((tmp_path / "fail").glob("*.error.json"))) == 1
    assert _failed_batches(db_session) == 1


def test_a_corrupt_pdf_is_quarantined_by_the_single_report_path(
    db_session, tmp_path, founding_org, monkeypatch
):
    # Reading the bytes is not a routing signal: whatever it raises, the file
    # takes the single-report path, which records the failed batch and
    # quarantines. The pack path is never offered.
    _no_pack_path(monkeypatch)
    bad = tmp_path / "inbox" / "corrupt.pdf"
    bad.parent.mkdir(exist_ok=True)
    bad.write_bytes(b"%PDF-1.4\nthis is not really a pdf\n")
    with pytest.raises(ProcessingError):
        process_document(
            db_session, bad, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
        )
    assert len(list((tmp_path / "fail").glob("*.error.json"))) == 1
    assert _failed_batches(db_session) == 1


def test_only_value_error_from_detection_selects_the_pack_path(db_session, tmp_path, monkeypatch):
    # Any other failure of the probe is not a routing signal and must
    # propagate rather than quietly send the file down the pack path.
    _seed(db_session, "skytouch")
    _no_pack_path(monkeypatch)

    def broken(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")
    monkeypatch.setattr(ingestion, "detect", broken)
    with pytest.raises(RuntimeError, match="registry unavailable"):
        process_document(
            db_session, _drop(tmp_path, PACK),
            processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
        )


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
    assert len(list((tmp_path / "fail").glob("*.error.json"))) == 1
    assert not (tmp_path / "done").exists()
    assert _failed_batches(db_session) == 1
