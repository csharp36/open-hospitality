from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from usali.models import PmsDailyStatisticStage
from usali.schemas import StatisticRecord
from usali.stats_stage import stage_statistics


@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def _rec(label: str, period: str, value: str, prior: bool = False) -> StatisticRecord:
    return StatisticRecord(
        property_id="HISJ",
        pms_source="OPERA",
        report_type="manager_flash",
        business_date=date(2026, 7, 7),
        metric_label=label,
        period_label=period,
        is_prior_year=prior,
        value=Decimal(value),
    )


def test_stage_statistics_inserts_and_batches(db_session):
    batch = stage_statistics(
        db_session, [_rec("ADR", "DAY", "167.66")], source_file="mf.pdf", file_hash="s1"
    )
    db_session.commit()
    assert isinstance(batch.batch_id, int)
    assert db_session.scalar(select(func.count()).select_from(PmsDailyStatisticStage)) == 1


def test_stage_statistics_is_idempotent(db_session):
    recs = [_rec("ADR", "DAY", "167.66"), _rec("ADR", "MONTH", "154.06")]
    stage_statistics(db_session, recs, source_file="mf.pdf", file_hash="dup")
    db_session.commit()
    stage_statistics(db_session, recs, source_file="mf.pdf", file_hash="dup")
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(PmsDailyStatisticStage)) == 2


def test_identical_values_in_one_report_both_survive(db_session):
    recs = [_rec("ADR", "DAY", "100.00"), _rec("ADR", "DAY", "100.00")]
    stage_statistics(db_session, recs, source_file="mf.pdf", file_hash="same")
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(PmsDailyStatisticStage)) == 2
