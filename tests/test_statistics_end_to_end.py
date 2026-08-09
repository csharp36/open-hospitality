import shutil
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from usali.ingestion import process_file
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import IngestBatch, UsaliFinancialFact, UsaliStatisticFact

SAMPLES = [
    "Trial Balance 07.07.2026 - Opera.pdf",
    "Autoclerk - Transaction Summary 07.07.2026.pdf",
    "Manager Flash 07.07.2026 - Opera.pdf",
    "Autoclerk - Manager Report 07.07.2026.pdf",
]


def test_all_four_reports_through_the_drop_folder(db_session, tmp_path):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    load_mappings(db_session, "mapping/autoclerk.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in SAMPLES:
        shutil.copy(Path("docs/reference/samples") / name, inbox / name)

    for pdf in sorted(inbox.glob("*.pdf")):
        process_file(
            db_session, pdf,
            processed_dir=tmp_path / "processed", failed_dir=tmp_path / "failed",
        )

    assert db_session.scalar(
        select(func.count()).select_from(UsaliFinancialFact)
    ) == 46  # 14 Opera + 32 Autoclerk, unchanged by statistics

    stat_facts = db_session.execute(select(UsaliStatisticFact)).scalars().all()
    assert len(stat_facts) == 103  # 84 Opera (incl. prior-year) + 19 Autoclerk
    by_key = {
        (f.pms_source, f.metric_code, f.period, f.is_prior_year): f.value for f in stat_facts
    }
    assert by_key[("OPERA", "ADR", "DAY", False)] == Decimal("167.66")
    assert by_key[("OPERA", "OCCUPANCY_PCT", "DAY", True)] == Decimal("79.27")
    assert by_key[("AUTOCLERK", "ADR", "DAY", False)] == Decimal("122.58")
    assert by_key[("AUTOCLERK", "REVPAR", "YTD", False)] == Decimal("95.11")
    # Both PMSs report the same canonical metric set — comparable side by side.
    opera_codes = {f.metric_code for f in stat_facts if f.pms_source == "OPERA"}
    autoclerk_codes = {f.metric_code for f in stat_facts if f.pms_source == "AUTOCLERK"}
    assert {"ADR", "REVPAR", "OCCUPANCY_PCT", "ROOMS_OCCUPIED"} <= (opera_codes & autoclerk_codes)

    statuses = set(db_session.execute(select(IngestBatch.status)).scalars())
    assert statuses == {"transformed"}
    assert len(list((tmp_path / "processed").glob("*.pdf"))) == 4
