import shutil
from pathlib import Path

from sqlalchemy import func, select

from usali.ingestion import process_file
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import IngestBatch, UsaliFinancialFact

SAMPLES = [
    Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf"),
    Path("docs/reference/samples/Autoclerk - Transaction Summary 07.07.2026.pdf"),
]


def test_both_sources_through_the_drop_folder(db_session, tmp_path):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    load_mappings(db_session, "mapping/autoclerk.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for sample in SAMPLES:
        shutil.copy(sample, inbox / sample.name)

    results = [
        process_file(
            db_session, pdf,
            processed_dir=tmp_path / "processed", failed_dir=tmp_path / "failed",
        )
        for pdf in sorted(inbox.glob("*.pdf"))
    ]

    by_source = {r.pms_source: r for r in results}
    assert by_source["OPERA"].mapped == 14
    assert by_source["AUTOCLERK"].mapped == 32
    assert all(r.unmapped == 0 for r in results)

    assert not list(inbox.glob("*.pdf"))
    assert len(list((tmp_path / "processed").glob("*.pdf"))) == 2

    fact_count = db_session.scalar(select(func.count()).select_from(UsaliFinancialFact))
    assert fact_count == 46
    statuses = set(db_session.execute(select(IngestBatch.status)).scalars())
    assert statuses == {"transformed"}
