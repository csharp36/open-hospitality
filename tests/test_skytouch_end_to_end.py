import shutil
from pathlib import Path

import pytest
from sqlalchemy import func, select

import usali.ingestion as ingestion
from usali.ingestion import ProcessingError, process_pack
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import IngestBatch, PmsDailyFinancialStage

SAMPLE = Path("docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf")


def test_skytouch_pack_end_to_end(db_session, tmp_path):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/skytouch.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    drop = tmp_path / SAMPLE.name
    shutil.copy(SAMPLE, drop)

    results = process_pack(
        db_session, drop, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail"
    )

    kinds = {(r.pms_source, r.report_type) for r in results}
    assert ("SKYTOUCH", "hotel_journal") in kinds
    # Hotel Statistics is un-registered (deferred to a real-sample calibration), so it is
    # skipped, not ingested.
    assert ("SKYTOUCH", "hotel_statistics") not in kinds
    assert all(r.property_id == "STDEMO" for r in results)
    # The pack has 3 sections (A/R Aging + Hotel Journal + Hotel Statistics). Exactly 1
    # result proves the unknown A/R Aging filler AND the un-registered Hotel Statistics
    # section were both dropped, and nothing extra leaked.
    assert len(results) == 1
    # The source file was filed to processed_dir, and every result's destination was
    # fixed up to point at the filed location (the dataclasses.replace after the move).
    assert (tmp_path / "done" / SAMPLE.name).exists()
    assert all(r.destination == tmp_path / "done" / SAMPLE.name for r in results)


def test_all_skipped_pack_raises_and_quarantines(db_session, tmp_path, founding_org):
    # Seed schedules + mappings but NOT properties: detect() matches each report
    # signature yet cannot resolve the property (ValueError), so every section is
    # skipped -> no results -> the pack is a failure. `founding_org` supplies org 1
    # (which seed_properties would otherwise create via ensure_default_org) so the
    # `failed` IngestBatch can be written without an FK violation.
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/skytouch.yaml")
    db_session.commit()

    drop = tmp_path / SAMPLE.name
    shutil.copy(SAMPLE, drop)

    with pytest.raises(ProcessingError):
        process_pack(
            db_session, drop, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail"
        )

    # Quarantined to failed_dir, never filed to processed_dir.
    assert (tmp_path / "fail" / SAMPLE.name).exists()
    assert not (tmp_path / "done" / SAMPLE.name).exists()
    # Exactly one failed batch recorded.
    failed = db_session.scalar(
        select(func.count()).select_from(IngestBatch).where(IngestBatch.status == "failed")
    )
    assert failed == 1


def test_pack_section_failure_rolls_back_and_quarantines(
    db_session, tmp_path, monkeypatch
):
    # Everything seeded, so the SkyTouch Hotel Journal section would normally succeed.
    # (Hotel Statistics is now un-registered and skipped, so the journal is the pack's
    # only recognized section.) Blow up the journal handler to exercise the pack failure
    # path: any staged work must roll back with the transaction, and the file is
    # quarantined rather than filed. Patch the _PIPELINES entry itself (not the module
    # attribute): the dict captured the handler reference at import, so _process_section
    # reads it from there, not the name.
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/skytouch.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    def _boom(*args, **kwargs):
        raise RuntimeError("journal handler exploded mid-pack")

    monkeypatch.setitem(ingestion._PIPELINES, ("SKYTOUCH", "hotel_journal"), _boom)

    drop = tmp_path / SAMPLE.name
    shutil.copy(SAMPLE, drop)

    with pytest.raises(ProcessingError):
        process_pack(
            db_session, drop, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail"
        )

    # No financial rows survived the rolled-back transaction.
    staged = db_session.scalar(
        select(func.count()).select_from(PmsDailyFinancialStage)
    )
    assert staged == 0
    # Exactly one failed batch, and the file is quarantined.
    failed = db_session.scalar(
        select(func.count()).select_from(IngestBatch).where(IngestBatch.status == "failed")
    )
    assert failed == 1
    assert (tmp_path / "fail" / SAMPLE.name).exists()
    assert not (tmp_path / "done" / SAMPLE.name).exists()
