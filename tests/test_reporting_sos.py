"""Summary Operating Statement queries (P6 Task 1) against real seeded data.

Each test re-ingests the six sample PDFs through `process_file` via the shared
`seed_six_pdfs` fixture (per-test TRUNCATE keeps isolation), then asserts on the SOS
report the reporting module builds from the promoted facts. One consolidated test
function per concern keeps runtime sane.
"""

from datetime import date
from decimal import Decimal

import pytest

from usali.models import IngestBatch, PmsDailyFinancialStage, UsaliFinancialFact
from usali.reporting import summary_operating_statement



@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def test_sos_financial_sections_opera(db_session, seed_six_pdfs):
    sos = summary_operating_statement(
        db_session, property_id="HISJ", business_date=date(2026, 7, 7)
    )

    assert sos.property_id == "HISJ"
    assert sos.pms_source == "OPERA"
    assert sos.business_date == date(2026, 7, 7)

    # Total Operating Revenue = every fact with a USALI schedule (1/3/4).
    assert sos.total_operating_revenue == Decimal("10866.37")

    # Operated Departments: Rooms first, then Other Operated Departments.
    rooms = next(s for s in sos.operated_departments if s.sub_category == "Rooms")
    assert rooms.total == Decimal("10395.00")
    assert sos.operated_departments[0].sub_category == "Rooms"

    misc = sos.misc_income_total  # Parking 410.00 (Sch 4)
    assert misc == Decimal("410.00")
    # Misc income exposes its per-line breakdown, not just the total.
    assert [(line.line_item, line.total) for line in sos.misc_income] == [
        ("Parking", Decimal("410.00"))
    ]

    # Taxes are pass-through: 5007+7100+7101+7102+7104.
    assert sos.taxes_total == Decimal("1573.29")
    assert all(line.major == "Taxes (Pass-Through)" for line in sos.taxes)

    assert sos.settlements_total == Decimal("-7093.48")
    assert all(line.major == "Settlements" for line in sos.settlements)

    # Rooms segment split present and reconciles to the Rooms line.
    seg = {s.segment: s for s in sos.rooms_segments}
    assert sum(
        (s.room_revenue for s in sos.rooms_segments), Decimal("0")
    ) == Decimal("10395.00")
    assert seg["TRANSIENT"].rooms == Decimal("58")

    # Statistics pivot carries DAY/MTD/YTD and prior-year (Opera has prior-year).
    adr = next(m for m in sos.statistics if m.metric_code == "ADR")
    assert adr.day == Decimal("167.66") and adr.mtd == Decimal("154.06")
    assert adr.day_prior == Decimal("137.19")


def test_sos_autoclerk(db_session, seed_six_pdfs):
    sos = summary_operating_statement(
        db_session, property_id="SSSJ", business_date=date(2026, 7, 7)
    )

    assert sos.pms_source == "AUTOCLERK"
    assert sos.total_operating_revenue == Decimal("6359.03")

    # Autoclerk statistics carry no prior-year columns.
    adr = next(m for m in sos.statistics if m.metric_code == "ADR")
    assert adr.day == Decimal("122.58") and adr.day_prior is None


def test_sos_range_mode_sums_financials(db_session, seed_six_pdfs):
    # A one-day range equals the single date; proves the SUM path works.
    sos = summary_operating_statement(
        db_session, property_id="HISJ",
        date_from=date(2026, 7, 1), date_to=date(2026, 7, 31),
    )

    assert sos.business_date is None
    assert sos.date_from == date(2026, 7, 1) and sos.date_to == date(2026, 7, 31)
    assert sos.total_operating_revenue == Decimal("10866.37")

    # Statistics in range mode come from the latest business date in range.
    adr = next(m for m in sos.statistics if m.metric_code == "ADR")
    assert adr.day == Decimal("167.66")


def _synthetic_fact(db_session, *, pms_source, schedule_id, row_hash, property_id="TEST"):
    """Insert one financial fact directly (with its required batch + stage rows)."""
    batch = IngestBatch(
        pms_source=pms_source, report_type="trial_balance",
        source_file="synthetic.pdf", file_hash="0" * 64,
    )
    db_session.add(batch)
    db_session.flush()
    stage = PmsDailyFinancialStage(
        property_id=property_id, pms_source=pms_source, report_type="trial_balance",
        business_date=date(2026, 7, 7), pms_trx_code="9999", raw_amount=Decimal("1.00"),
        source_file="synthetic.pdf", ingest_batch_id=batch.batch_id, row_hash=row_hash,
    )
    db_session.add(stage)
    db_session.flush()
    db_session.add(UsaliFinancialFact(
        property_id=property_id, pms_source=pms_source, business_date=date(2026, 7, 7),
        usali_edition=12, usali_schedule_id=schedule_id,
        usali_major_category="Synthetic", usali_sub_category="Synthetic",
        usali_line_item="Synthetic", amount=Decimal("1.00"),
        ingest_batch_id=batch.batch_id, stage_id=stage.stage_id,
    ))
    db_session.flush()


def test_sos_rejects_unexpected_schedule_id(db_session):
    # Schedule 5 (A&G) is an expense schedule — must fail loudly, never count as revenue.
    _synthetic_fact(db_session, pms_source="OPERA", schedule_id=5, row_hash="a" * 64)

    with pytest.raises(ValueError, match=r"unexpected USALI schedule ids \[5\]"):
        summary_operating_statement(
            db_session, property_id="TEST", business_date=date(2026, 7, 7)
        )


def test_sos_rejects_multi_source_property(db_session):
    _synthetic_fact(db_session, pms_source="OPERA", schedule_id=1, row_hash="b" * 64)
    _synthetic_fact(db_session, pms_source="AUTOCLERK", schedule_id=1, row_hash="c" * 64)

    with pytest.raises(ValueError, match="multiple PMS sources"):
        summary_operating_statement(
            db_session, property_id="TEST", business_date=date(2026, 7, 7)
        )


def test_sos_argument_validation(db_session):
    d = date(2026, 7, 7)

    # Both modes given.
    with pytest.raises(ValueError, match="not both"):
        summary_operating_statement(
            db_session, property_id="TEST", business_date=d, date_from=d, date_to=d
        )
    # Neither mode given.
    with pytest.raises(ValueError, match="business_date, or both"):
        summary_operating_statement(db_session, property_id="TEST")
    # Partial range: from without to, and to without from.
    with pytest.raises(ValueError, match="business_date, or both"):
        summary_operating_statement(db_session, property_id="TEST", date_from=d)
    with pytest.raises(ValueError, match="business_date, or both"):
        summary_operating_statement(db_session, property_id="TEST", date_to=d)
    # Inverted range.
    with pytest.raises(ValueError, match="after date_to"):
        summary_operating_statement(
            db_session, property_id="TEST",
            date_from=date(2026, 7, 31), date_to=date(2026, 7, 1),
        )
    # No facts for the property/date (empty database).
    with pytest.raises(ValueError, match="no financial facts"):
        summary_operating_statement(db_session, property_id="NOPE", business_date=d)
