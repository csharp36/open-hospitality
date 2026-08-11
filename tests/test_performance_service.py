from datetime import date
from decimal import Decimal

from usali.models import (
    IngestBatch,
    Organization,
    PmsDailyStatisticStage,
    Property,
    UsaliStatisticFact,
)
from usali.performance import _stat_by_day


def _prop(session, pid="HISJ"):
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def _stat(session, pid, d, code, value):
    # The fact's FKs (ingest_batch_id, stat_stage_id) are NOT NULL, and
    # stat_stage_id is uniquely constrained, so each fact needs its own staged
    # provenance row for the insert to satisfy the schema.
    batch = IngestBatch(pms_source="OPERA", report_type="manager_flash",
                        source_file="t", file_hash="h", status="staged",
                        row_count=1, error_count=0)
    session.add(batch)
    session.flush()
    stage = PmsDailyStatisticStage(
        property_id=pid, pms_source="OPERA", report_type="manager_flash",
        business_date=d, metric_label=code, period_label="DAY",
        is_prior_year=False, value=value, source_file="t",
        ingest_batch_id=batch.batch_id, row_hash=f"{code}-{d}",
    )
    session.add(stage)
    session.flush()
    return UsaliStatisticFact(property_id=pid, pms_source="OPERA", business_date=d,
                              metric_code=code, period="DAY", is_prior_year=False,
                              value=value, ingest_batch_id=batch.batch_id,
                              stat_stage_id=stage.stat_stage_id)


def test_stat_by_day_returns_daily_values(db_session):
    _prop(db_session)
    db_session.add_all([
        _stat(db_session, "HISJ", date(2026, 1, 1), "ROOM_REVENUE", 10000),
        _stat(db_session, "HISJ", date(2026, 1, 2), "ROOM_REVENUE", 12000),
    ])
    db_session.commit()
    got = _stat_by_day(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 2), "ROOM_REVENUE")
    assert got == {date(2026, 1, 1): Decimal("10000"), date(2026, 1, 2): Decimal("12000")}
