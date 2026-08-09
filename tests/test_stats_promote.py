from datetime import date
from decimal import Decimal

from sqlalchemy import select

import pytest

from usali.models import UsaliStatisticFact
from usali.schemas import StatisticRecord
from usali.stats_promote import promote_statistics
from usali.stats_stage import stage_statistics


@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def _rec(label: str, period: str, value: str, prior: bool = False) -> StatisticRecord:
    return StatisticRecord(
        property_id="HISJ", pms_source="OPERA", report_type="manager_flash",
        business_date=date(2026, 7, 7), metric_label=label, period_label=period,
        is_prior_year=prior, value=Decimal(value),
    )


def test_promotes_curated_metrics_and_canonical_periods(db_session):
    stage_statistics(
        db_session,
        [
            _rec("ADR", "DAY", "167.66"),
            _rec("ADR", "MONTH", "154.06"),          # MONTH -> MTD
            _rec("ADR", "YEAR", "162.43", prior=True),  # YEAR -> YTD, prior kept
            _rec("Clean Rooms", "DAY", "0"),         # uncurated -> not promoted
        ],
        source_file="mf.pdf", file_hash="p1",
    )
    db_session.commit()
    result = promote_statistics(
        db_session, "mapping/statistics.yaml", source="OPERA", business_date=date(2026, 7, 7)
    )
    db_session.commit()
    assert result.promoted == 3
    assert result.ignored == 1
    facts = db_session.execute(select(UsaliStatisticFact)).scalars().all()
    by_key = {(f.metric_code, f.period, f.is_prior_year): f.value for f in facts}
    assert by_key[("ADR", "DAY", False)] == Decimal("167.66")
    assert by_key[("ADR", "MTD", False)] == Decimal("154.06")
    assert by_key[("ADR", "YTD", True)] == Decimal("162.43")


def test_promotion_is_idempotent(db_session):
    stage_statistics(
        db_session, [_rec("ADR", "DAY", "167.66")], source_file="mf.pdf", file_hash="p2"
    )
    db_session.commit()
    promote_statistics(db_session, "mapping/statistics.yaml", source="OPERA",
                       business_date=date(2026, 7, 7))
    db_session.commit()
    second = promote_statistics(db_session, "mapping/statistics.yaml", source="OPERA",
                                business_date=date(2026, 7, 7))
    db_session.commit()
    assert second.promoted == 0 and second.skipped == 1
    assert len(db_session.execute(select(UsaliStatisticFact)).scalars().all()) == 1
