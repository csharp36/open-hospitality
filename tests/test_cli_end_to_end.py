from datetime import date

import pytest
from sqlalchemy import func, select

from usali.adaptors.opera_trial_balance import parse_trial_balance
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


SAMPLE = "docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf"


def test_full_pipeline_on_real_pdf(db_session):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    db_session.commit()

    words = extract_words(SAMPLE)
    records = parse_trial_balance(words, property_id="HISJ", business_date=date(2026, 7, 7))
    stage_records(db_session, records, source_file=SAMPLE, file_hash="e2e")
    db_session.commit()

    result = transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()

    # Every code in the sample Trial Balance is covered by the curated mapping.
    assert result.unmapped == 0
    assert result.reconciled is True

    # No staged row fell through to the mapping-exception table.
    assert db_session.scalar(select(func.count()).select_from(MappingException)) == 0

    # The accommodation revenue landed on the Rooms line.
    room_rev = db_session.scalar(
        select(func.sum(UsaliFinancialFact.amount)).where(
            UsaliFinancialFact.usali_line_item == "Room Revenue"
        )
    )
    assert room_rev == 10395.00
