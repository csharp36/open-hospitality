from usali.models import (
    Base,
    IngestBatch,
    MappingException,
    PmsDailyFinancialStage,
    UsaliFinancialFact,
    UsaliMappingDictionary,
    UsaliSchedule,
)


def test_tables_registered():
    names = set(Base.metadata.tables)
    assert names == {
        "pms_daily_financial_stage",
        "usali_mapping_dictionary",
        "usali_schedule",
        "usali_financial_fact",
        "mapping_exception",
        "ingest_batch",
        "pms_daily_statistic_stage",
        "usali_statistic_fact",
        "pms_ledger_balance_stage",
        "usali_ledger_balance_fact",
        "qbo_push_ledger",
        "pms_daily_segment_stage",
        "usali_segment_fact",
        "usali_labor_fact",
        "organization",
        "invite",
        "otp_challenge",
        "pms_interest_request",
        "org_settings",
        "property",
        "property_detection_alias",
        "department",
        "position",
        "employee",
        "employee_assignment",
        "employee_face_template",
        "role_assignment",
        "assignment_rate",
        "audit_event",
        "kiosk_device",
        "punch",
        "timecard",
        "timecard_adjustment",
        "employee_payroll_profile",
        "deposit_account",
        "sick_leave_ledger",
        "pay_schedule",
        "pay_run",
        "pay_run_line",
        "pay_run_line_property",
        "wage_settlement",
        "crm_pull_batch",
        "crm_demand_snapshot",
        "provider_employee_ref",
        "usali_actual_labor_fact",
        "shift_template",
        "schedule",
        "shift",
        "labor_standard",
        "occupancy_forecast",
        "room_inventory",
        "out_of_order_room",
        "fiscal_calendar",
        "property_stat_config",
        "ingestion_coverage",
    }


def test_mapping_dictionary_unique_key():
    uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in UsaliMappingDictionary.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("pms_source", "pms_trx_code", "usali_edition") in {
        tuple(sorted(u)) for u in uniques
    }


def test_outputs_track_stage_id_uniquely():
    fact_uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in UsaliFinancialFact.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    exc_uniques = {
        tuple(sorted(c.name for c in con.columns))
        for con in MappingException.__table__.constraints
        if con.__class__.__name__ == "UniqueConstraint"
    }
    assert ("stage_id",) in fact_uniques
    assert ("stage_id",) in exc_uniques
    assert MappingException.__table__.c.stage_id.nullable is False
