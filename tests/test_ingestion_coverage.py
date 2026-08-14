from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from usali.models import IngestionCoverage, Organization, Property


def _property(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def test_coverage_is_unique_per_property_day_report(db_session):
    _property(db_session)
    db_session.add(IngestionCoverage(property_id="HISJ", business_date=date(2026, 7, 7),
                                     report_type="manager_flash"))
    db_session.commit()
    db_session.add(IngestionCoverage(property_id="HISJ", business_date=date(2026, 7, 7),
                                     report_type="manager_flash"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_process_file_records_coverage(db_session, tmp_path):
    import shutil
    from pathlib import Path

    from usali.ingestion import process_file
    from usali.mapping.loader import load_mappings
    from usali.mapping.property_registry import ensure_default_org, seed_properties
    from usali.mapping.schedules import seed_schedules

    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    ensure_default_org(db_session)
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    name = "Manager Flash 07.07.2026 - Opera.pdf"
    shutil.copy(Path("docs/reference/samples") / name, inbox / name)
    process_file(db_session, inbox / name, processed_dir=tmp_path / "p", failed_dir=tmp_path / "f")
    db_session.commit()
    rows = db_session.execute(select(IngestionCoverage)).scalars().all()
    assert any(r.business_date == date(2026, 7, 7) for r in rows)
    assert any(r.report_type == "manager_flash" for r in rows)
