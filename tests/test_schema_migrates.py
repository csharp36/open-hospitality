from sqlalchemy import inspect


def test_all_tables_created(db_engine):
    tables = set(inspect(db_engine).get_table_names())
    assert {
        "pms_daily_financial_stage",
        "usali_mapping_dictionary",
        "usali_schedule",
        "usali_financial_fact",
        "mapping_exception",
        "ingest_batch",
        "organization",
        "property",
        "property_detection_alias",
        "department",
        "position",
        "employee",
        "role_assignment",
        "audit_event",
        "kiosk_device",
        "punch",
        "timecard",
        "timecard_adjustment",
        "usali_labor_fact",
        "employee_payroll_profile",
        "pay_schedule",
        "pay_run",
        "pay_run_line",
        "provider_employee_ref",
        "usali_actual_labor_fact",
        "shift_template",
        "schedule",
        "shift",
        "labor_standard",
        "occupancy_forecast",
    } <= tables
