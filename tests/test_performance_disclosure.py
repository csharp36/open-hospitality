"""The disclosure gate on `labor_productivity` (issue #9, Task B4).

Labor-COST per occupied room is per-employee MONEY, so it MUST compose with the
SAME per-day `_discloses` guard the SOS uses: a day whose cost came from a single
priced employee re-derives that person's encrypted, payroll-admin-gated pay rate
as cost / hours, so the WHOLE cost figure is withheld while the pure operational
HOURS stay complete. Seeded through the real promote path (make_employee ->
punches -> approved timecard -> promote_timecard) so the `UsaliLaborFact` rows
carry genuine est_cost and a real timecard->employee identity, which is what the
gate counts. No `seed_six_pdfs`: `labor_productivity` reads labor + statistics
only, never the PMS revenue facts.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from tests.employees import make_employee
from usali.labor import promote_timecard
from usali.models import (
    Department,
    IngestBatch,
    KioskDevice,
    Organization,
    PmsDailyStatisticStage,
    Position,
    Property,
    Punch,
    UsaliStatisticFact,
)
from usali.performance import labor_productivity
from usali.timecards import assemble_timecard

# A Monday — the workweek/period grid anchor — and the day worked.
_ANCHOR = date(2026, 1, 5)
_DAY = date(2026, 1, 5)


def _seed_property(db_session):
    db_session.merge(Organization(org_id=1, name="Org"))
    # wage_jurisdiction is REQUIRED to cost hours: a NULL one reaches rules_for
    # and refuses (labor.py), so the promote would not price anything.
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping",
                      usali_schedule_id=14, usali_edition=12)
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Attendant", flsa_exempt=False)
    db_session.add(pos)
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64, enrolled_by="a")
    db_session.add_all([pos, device])
    db_session.flush()
    return dept, pos, device


def _rooms_occupied(db_session, d, value):
    batch = IngestBatch(pms_source="OPERA", report_type="manager_flash", source_file="t",
                        file_hash="h", status="staged", row_count=1, error_count=0)
    db_session.add(batch)
    db_session.flush()
    stage = PmsDailyStatisticStage(
        property_id="HISJ", pms_source="OPERA", report_type="manager_flash",
        business_date=d, metric_label="ROOMS_OCCUPIED", period_label="DAY",
        is_prior_year=False, value=value, source_file="t",
        ingest_batch_id=batch.batch_id, row_hash=f"ro-{d}",
    )
    db_session.add(stage)
    db_session.flush()
    db_session.add(UsaliStatisticFact(
        property_id="HISJ", pms_source="OPERA", business_date=d,
        metric_code="ROOMS_OCCUPIED", period="DAY", is_prior_year=False, value=value,
        ingest_batch_id=batch.batch_id, stat_stage_id=stage.stat_stage_id))
    db_session.flush()


def _priced_worker(db_session, dept, pos, device, name, *, pay_rate, day=_DAY):
    """One employee with an 8h day on `day`, priced at `pay_rate`, promoted —
    so their hours land as a priced `UsaliLaborFact` (est_cost > 0), which is
    exactly the population `_discloses` counts."""
    emp = make_employee(db_session, property_id="HISJ", department_id=dept.department_id,
                        position_id=pos.position_id, full_name=name, pay_type="hourly",
                        pay_rate=pay_rate)
    db_session.flush()
    for ptype, h in (("clock_in", 9), ("clock_out", 17)):
        db_session.add(Punch(
            employee_id=emp.employee_id, kiosk_device_id=device.device_id, punch_type=ptype,
            punched_at=datetime(day.year, day.month, day.day, h, tzinfo=UTC),
            business_date=day, photo_key=f"k/{name}/{ptype}"))
    db_session.commit()
    card = assemble_timecard(db_session, emp.employee_id, day, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    return emp


def test_solo_priced_day_suppresses_cost_but_not_hours(db_session):
    # ONE priced employee funds the day, so cost / hours re-derives their exact
    # rate — cost must be withheld. Hours are operational and always survive.
    dept, pos, device = _seed_property(db_session)
    _rooms_occupied(db_session, _DAY, 80)
    _priced_worker(db_session, dept, pos, device, "Solo S", pay_rate="20.00")

    p = labor_productivity(db_session, "HISJ", _DAY, _DAY)

    assert p.cost_suppressed is True
    assert p.labor_cost is None
    assert p.cost_per_occupied_room is None
    # Hours are NEVER suppressed — Schedule 15 detail stays complete.
    assert p.labor_hours == Decimal("8.00")
    assert p.hours_per_occupied_room == Decimal("0.1000")  # 8/80


def test_two_priced_employees_disclose_cost(db_session):
    # TWO distinct priced employees fund the day, so the total is a >=2-person
    # aggregate and no single rate is recoverable — cost discloses.
    dept, pos, device = _seed_property(db_session)
    _rooms_occupied(db_session, _DAY, 80)
    _priced_worker(db_session, dept, pos, device, "Ann A", pay_rate="20.00")
    _priced_worker(db_session, dept, pos, device, "Ben B", pay_rate="25.00")

    p = labor_productivity(db_session, "HISJ", _DAY, _DAY)

    assert p.cost_suppressed is False
    # 8h × $20 + 8h × $25 = 160 + 200 = 360.
    assert p.labor_cost == Decimal("360.0000")
    assert p.cost_per_occupied_room == Decimal("4.5000")  # 360/80
    assert p.labor_hours == Decimal("16.00")
    assert p.hours_per_occupied_room == Decimal("0.2000")  # 16/80
