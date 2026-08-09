from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee
from usali.labor import promote_timecard
from usali.models import (
    AssignmentRate,
    Department,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
    UsaliLaborFact,
)
from usali.timecards import assemble_timecard
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)  # Monday


def _seed(db_session, *, exempt=False, pay_rate="20.00"):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Room Attendant", flsa_exempt=exempt)
    db_session.add(pos)
    db_session.flush()
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64, enrolled_by="adm")
    emp = make_employee(db_session, property_id="HISJ", department_id=dept.department_id,
                   position_id=pos.position_id, full_name="Hank H", pay_type="hourly",
                   pay_rate=pay_rate)
    db_session.add_all([device, emp])
    db_session.flush()
    return dept.department_id, device.device_id, emp.employee_id


def _shift(db_session, device_id, emp_id, day, in_h, out_h):
    for ptype, h in (("clock_in", in_h), ("clock_out", out_h)):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(2026, 1, day, h, tzinfo=UTC), business_date=date(2026, 1, day),
            photo_key=f"k/{ptype}{day}{h}",
        ))


def _approved_card(db_session, emp_id):
    card = assemble_timecard(db_session, emp_id, date(2026, 1, 5), anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    return card


def test_promote_writes_department_aggregate_cost_and_hours(db_session):
    dept_id, device_id, emp_id = _seed(db_session, pay_rate="20.00")
    # Two ordinary 8h days in week 1 → 16h, all regular, no OT.
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    _shift(db_session, device_id, emp_id, 6, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id)

    n = promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = db_session.execute(select(UsaliLaborFact)).scalars().all()
    assert n == len(facts) == 2  # one per business date
    by_date = {f.business_date: f for f in facts}
    day5 = by_date[date(2026, 1, 5)]
    assert Decimal(str(day5.hours)) == Decimal("8.00")
    assert Decimal(str(day5.ot_hours)) == Decimal("0.00")
    assert Decimal(str(day5.est_cost)) == Decimal("160.0000")  # 8h × $20
    assert day5.department_id == dept_id


def test_promote_applies_overtime_cost(db_session):
    _dept, device_id, emp_id = _seed(db_session, pay_rate="20.00")
    _shift(db_session, device_id, emp_id, 5, 8, 20)  # 12h → 8 reg + 4 OT
    db_session.commit()
    card = _approved_card(db_session, emp_id)
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    f = db_session.execute(select(UsaliLaborFact)).scalars().one()
    # 8×20 + 4×20×1.5 = 160 + 120 = 280
    assert Decimal(str(f.est_cost)) == Decimal("280.0000")
    assert Decimal(str(f.ot_hours)) == Decimal("4.00")


def test_promote_is_idempotent(db_session):
    _dept, device_id, emp_id = _seed(db_session)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id)
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    promote_timecard(db_session, card, anchor=_ANCHOR)  # again
    db_session.commit()
    assert len(db_session.execute(select(UsaliLaborFact)).scalars().all()) == 1


def test_promote_refuses_unapproved_card(db_session):
    _dept, device_id, emp_id = _seed(db_session)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    db_session.commit()
    card = assemble_timecard(db_session, emp_id, date(2026, 1, 5), anchor=_ANCHOR)  # still open
    db_session.commit()
    try:
        promote_timecard(db_session, card, anchor=_ANCHOR)
        raise AssertionError("expected a refusal")
    except ValueError:
        pass
    assert db_session.execute(select(UsaliLaborFact)).scalars().all() == []


def test_promote_without_pay_rate_records_hours_but_zero_cost(db_session):
    _dept, device_id, emp_id = _seed(db_session, pay_rate=None)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id)
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    f = db_session.execute(select(UsaliLaborFact)).scalars().one()
    assert Decimal(str(f.hours)) == Decimal("8.00")   # Schedule 15 still complete
    assert Decimal(str(f.est_cost)) == Decimal("0.0000")  # Schedule 14 fills in once a rate is set


def test_promote_exempt_employee_records_hours_but_zero_cost(db_session):
    # The estimate prices HOURLY labor. An exempt (salaried) employee's hours
    # still promote (Schedule 15), but are never costed hours×rate — even when
    # an hourly-looking pay_rate is on file.
    _dept, device_id, emp_id = _seed(db_session, exempt=True, pay_rate="30.00")
    _shift(db_session, device_id, emp_id, 5, 8, 20)  # 12h, but exempt → no OT
    db_session.commit()
    card = _approved_card(db_session, emp_id)
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    f = db_session.execute(select(UsaliLaborFact)).scalars().one()
    assert Decimal(str(f.hours)) == Decimal("12.00")
    assert Decimal(str(f.ot_hours)) == Decimal("0.00")
    assert Decimal(str(f.est_cost)) == Decimal("0.0000")  # salary is not a wage


def test_promote_unparseable_pay_rate_fails_as_value_error(db_session):
    """CorruptRateError subclasses ValueError on purpose. `promote_timecard`
    documented this failure as the ValueError callers already handle, and a bare
    Exception would walk that back to a 500 with no clean rollback."""

    _dept, device_id, emp_id = _seed(db_session, pay_rate="20.00")
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id)
    # Corrupt the RATE ROW, which is where compensation lives since E2.
    rate = db_session.execute(select(AssignmentRate)).scalars().one()
    rate.amount = "corrupt-rate!!"  # simulate a bad decrypt / bad write
    db_session.flush()

    try:
        promote_timecard(db_session, card, anchor=_ANCHOR)
        raise AssertionError("expected ValueError for unparseable pay rate")
    except ValueError as exc:
        msg = str(exc)
        assert str(emp_id) in msg          # names the employee...
        assert "corrupt-rate!!" not in msg  # ...but never the rate (compensation data)
    db_session.rollback()
    assert db_session.execute(select(UsaliLaborFact)).scalars().all() == []
