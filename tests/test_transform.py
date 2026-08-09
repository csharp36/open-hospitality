from datetime import date
from decimal import Decimal

from sqlalchemy import select

import pytest

from usali.mapping.loader import load_mappings
from usali.models import MappingException, UsaliFinancialFact
from usali.schemas import StagedRecord
from usali.stage import stage_records
from usali.transform import transform


@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def _rec(code: str, amt: str, section: str) -> StagedRecord:
    return StagedRecord(
        property_id="HISJ", pms_source="OPERA", report_type="trial_balance",
        business_date=date(2026, 7, 7), pms_trx_code=code, pms_trx_desc="d",
        raw_amount=Decimal(amt), section=section,
    )


def test_transform_maps_known_and_flags_unknown(db_session):
    load_mappings(db_session, "mapping/opera.yaml")
    stage_records(
        db_session,
        [_rec("1000", "10395.00", "Revenue"), _rec("8888", "12.00", "Revenue")],
        source_file="tb.pdf", file_hash="tf1",
    )
    db_session.commit()

    result = transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()

    facts = db_session.execute(
        select(UsaliFinancialFact).where(UsaliFinancialFact.pms_source == "OPERA")
    ).scalars().all()
    # The fact table intentionally carries USALI classification fields, NOT the raw PMS code.
    assert not hasattr(UsaliFinancialFact, "pms_trx_code")
    assert any(f.usali_line_item == "Room Revenue" and f.amount == Decimal("10395.00") for f in facts)

    exc = db_session.execute(
        select(MappingException).where(MappingException.pms_trx_code == "8888")
    ).scalars().one()
    assert exc.raw_amount == Decimal("12.00")
    assert result.mapped == 1
    assert result.unmapped == 1


def test_transform_reconciles_totals(db_session):
    load_mappings(db_session, "mapping/opera.yaml")
    stage_records(
        db_session,
        [_rec("1000", "100.00", "Revenue"), _rec("9004", "-40.00", "Payment")],
        source_file="tb2.pdf", file_hash="tf2",
    )
    db_session.commit()
    result = transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()
    assert result.reconciled is True


def test_transform_mixed_batch_reconciles(db_session):
    load_mappings(db_session, "mapping/opera.yaml")
    stage_records(
        db_session,
        [
            _rec("1000", "100.00", "Revenue"),   # mapped
            _rec("9004", "-40.00", "Payment"),   # mapped, negative
            _rec("8888", "-15.00", "Revenue"),   # unmapped, negative
        ],
        source_file="mix.pdf", file_hash="mix1",
    )
    db_session.commit()
    result = transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()
    assert result.mapped == 2
    assert result.unmapped == 1
    assert result.reconciled is True


def test_transform_rerun_skips_processed_rows(db_session):
    load_mappings(db_session, "mapping/opera.yaml")
    stage_records(
        db_session,
        [_rec("1000", "100.00", "Revenue"), _rec("8888", "-15.00", "Revenue")],
        source_file="r.pdf", file_hash="r1",
    )
    db_session.commit()
    first = transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()
    assert (first.mapped, first.unmapped, first.skipped) == (1, 1, 0)

    second = transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()
    assert (second.mapped, second.unmapped, second.skipped) == (0, 0, 2)
    assert second.reconciled is True

    facts = db_session.execute(select(UsaliFinancialFact)).scalars().all()
    excs = db_session.execute(select(MappingException)).scalars().all()
    assert len(facts) == 1 and len(excs) == 1  # no duplicates from the rerun


def test_transform_partial_rerun_processes_only_new_rows(db_session):
    # Realistic incremental case: a second file lands for the same (source, date)
    # after a transform already ran — only the new rows get processed.
    load_mappings(db_session, "mapping/opera.yaml")
    stage_records(
        db_session, [_rec("1000", "100.00", "Revenue")], source_file="a.pdf", file_hash="pa1"
    )
    db_session.commit()
    transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()

    stage_records(
        db_session, [_rec("9004", "-40.00", "Payment")], source_file="b.pdf", file_hash="pb1"
    )
    db_session.commit()
    result = transform(db_session, source="OPERA", business_date=date(2026, 7, 7), edition=12)
    db_session.commit()

    assert (result.mapped, result.unmapped, result.skipped) == (1, 0, 1)
    assert result.reconciled is True
    facts = db_session.execute(select(UsaliFinancialFact)).scalars().all()
    assert len(facts) == 2  # one per staged row, no duplicates
