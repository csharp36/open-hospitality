from usali.models import Base, PmsDailySegmentStage, UsaliSegmentFact


def test_segment_tables_registered():
    assert "pms_daily_segment_stage" in Base.metadata.tables
    assert "usali_segment_fact" in Base.metadata.tables


def test_segment_stage_unique_row_hash():
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in PmsDailySegmentStage.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    # L8-F4: org_id joined the unique so a cross-org row_hash collision no
    # longer silently drops the second org's row.
    assert ("business_date", "org_id", "pms_source", "row_hash") in uniques


def test_segment_fact_unique_grain():
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in UsaliSegmentFact.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert (
        "business_date", "period", "pms_source", "property_id", "usali_segment"
    ) in uniques
