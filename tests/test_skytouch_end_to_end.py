import shutil
from pathlib import Path

from usali.ingestion import process_pack
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules


def test_skytouch_pack_end_to_end(db_session, tmp_path):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/skytouch.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    src = Path("docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf")
    drop = tmp_path / src.name
    shutil.copy(src, drop)

    results = process_pack(
        db_session, drop, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail"
    )

    kinds = {(r.pms_source, r.report_type) for r in results}
    assert ("SKYTOUCH", "hotel_journal") in kinds
    assert ("SKYTOUCH", "hotel_statistics") in kinds
    assert all(r.property_id == "STDEMO" for r in results)
    # the A/R Aging filler section is not a known report -> skipped, not a result
    assert ("SKYTOUCH", "A/R Aging") not in kinds
    # the source file was filed to processed_dir
    assert (tmp_path / "done" / src.name).exists()
