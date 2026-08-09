from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from usali.adaptors.autoclerk_transaction_summary import (
    extract_business_date,
    parse_transaction_summary,
)
from usali.adaptors.pdf import extract_words
from usali.mapping.loader import load_mappings
from usali.mapping.schedules import seed_schedules
from usali.models import MappingException, UsaliFinancialFact
from usali.stage import stage_records
from usali.transform import transform


@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


SAMPLE = "docs/reference/samples/Autoclerk - Transaction Summary 07.07.2026.pdf"


def test_full_pipeline_on_real_autoclerk_pdf(db_session):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/autoclerk.yaml")
    db_session.commit()

    words = extract_words(SAMPLE)
    business_date = extract_business_date(words)
    assert business_date == date(2026, 7, 7)

    records = parse_transaction_summary(words, property_id="SSSJ", business_date=business_date)
    stage_records(db_session, records, source_file=SAMPLE, file_hash="ac-e2e")
    db_session.commit()

    result = transform(db_session, source="AUTOCLERK", business_date=business_date, edition=12)
    db_session.commit()

    # Every Autoclerk code in the sample is covered by the curated mapping.
    assert result.unmapped == 0
    assert result.reconciled is True
    assert db_session.scalar(select(func.count()).select_from(MappingException)) == 0

    # Room Rent landed on the Rooms "Room Revenue" line at the real amount.
    room_rev = db_session.scalar(
        select(func.sum(UsaliFinancialFact.amount)).where(
            UsaliFinancialFact.usali_line_item == "Room Revenue",
            UsaliFinancialFact.pms_source == "AUTOCLERK",
        )
    )
    assert room_rev == Decimal("6129.03")
