from usali.models import Base, PmsDailyStatisticStage, UsaliStatisticFact


def test_statistics_tables_registered():
    assert "pms_daily_statistic_stage" in Base.metadata.tables
    assert "usali_statistic_fact" in Base.metadata.tables


def test_statistic_stage_unique_row_hash():
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in PmsDailyStatisticStage.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    # L8-F4: org_id joined the unique so a cross-org row_hash collision no
    # longer silently drops the second org's row.
    assert ("business_date", "org_id", "pms_source", "row_hash") in uniques


def test_statistic_fact_tracks_stage_uniquely():
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in UsaliStatisticFact.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("stat_stage_id",) in uniques
