"""Which property carries each worked minute.

Hours attribute to the property whose KIOSK recorded the punch -- a link present
since B1 and never read. NOT to the employee's primary property, which is what
every pre-E1 caller assumed and which is wrong for the 21 of 28 pilot staff who
work both hotels.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tests.employees import make_employee
from usali.attribution import (
    UnattributableHoursError,
    property_hours_for_timecard,
    property_minutes_for_day,
)
from usali.models import (
    EmployeeAssignment,
    KioskDevice,
    Organization,
    Property,
    Punch,
    Timecard,
    TimecardAdjustment,
)
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_DAY = date(2026, 1, 6)


def _at(hour, minute=0):
    return datetime(2026, 1, 6, hour, minute, tzinfo=timezone.utc)


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="58033", org_id=1, name="BW", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    sjces_kiosk = KioskDevice(
        property_id="SJCES", name="SJCES iPad", token_hash="a" * 64, enrolled_by="admin"
    )
    bw_kiosk = KioskDevice(
        property_id="58033", name="BW iPad", token_hash="b" * 64, enrolled_by="admin"
    )
    emp = make_employee(db_session, place_primary=False, property_id="SJCES", full_name="Vikram Jindal", pay_type="hourly")
    db_session.add_all([sjces_kiosk, bw_kiosk, emp])
    db_session.flush()
    card = Timecard(
        employee_id=emp.employee_id, period_start=_ANCHOR,
        period_end=_ANCHOR + timedelta(days=13), status="open",
    )
    db_session.add(card)
    db_session.flush()
    return emp, card, sjces_kiosk, bw_kiosk


def _span(db_session, card, emp, device, start_hour, end_hour):
    db_session.add_all([
        Punch(employee_id=emp.employee_id, kiosk_device_id=device.device_id,
              punch_type="clock_in", punched_at=_at(start_hour), business_date=_DAY,
              timecard_id=card.timecard_id),
        Punch(employee_id=emp.employee_id, kiosk_device_id=device.device_id,
              punch_type="clock_out", punched_at=_at(end_hour), business_date=_DAY,
              timecard_id=card.timecard_id),
    ])


def test_hours_split_across_properties_by_kiosk_device(db_session):
    """THE case the design turns on: 6h at one hotel, 5h at the other, one day."""
    emp, card, sjces, bw = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)   # 6h at SJCES
    _span(db_session, card, emp, bw, 13, 18)     # 5h at 58033
    db_session.commit()

    assert property_hours_for_timecard(db_session, card) == {
        "SJCES": Decimal("6.00"),
        "58033": Decimal("5.00"),
    }


def test_attribution_ignores_the_employees_primary_property(db_session):
    """Primary is SJCES, but the whole period was worked at 58033. SJCES must
    carry ZERO -- attributing by the employee row is the bug this replaces."""
    emp, card, _sjces, bw = _seed(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES", is_primary=True,
        status="active", effective_from=_ANCHOR,
    ))
    _span(db_session, card, emp, bw, 9, 17)
    db_session.commit()

    hours = property_hours_for_timecard(db_session, card)
    assert hours == {"58033": Decimal("8.00")}
    assert "SJCES" not in hours


def test_a_span_belongs_to_where_it_started(db_session):
    """Clocking out at the other hotel does not move the shift there."""
    emp, card, sjces, bw = _seed(db_session)
    db_session.add_all([
        Punch(employee_id=emp.employee_id, kiosk_device_id=sjces.device_id,
              punch_type="clock_in", punched_at=_at(9), business_date=_DAY,
              timecard_id=card.timecard_id),
        Punch(employee_id=emp.employee_id, kiosk_device_id=bw.device_id,
              punch_type="clock_out", punched_at=_at(17), business_date=_DAY,
              timecard_id=card.timecard_id),
    ])
    db_session.commit()
    assert property_hours_for_timecard(db_session, card) == {"SJCES": Decimal("8.00")}


def test_lunch_is_deducted_proportionally_not_charged_to_one_hotel(db_session):
    """A meal break has no property of its own. Deducting it entirely from
    whichever hotel happened to contain it would shift cost between P&Ls for a
    reason that has nothing to do with where work was done."""
    emp, card, sjces, bw = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)   # 6h gross
    _span(db_session, card, emp, bw, 13, 19)     # 6h gross
    db_session.add_all([
        Punch(employee_id=emp.employee_id, kiosk_device_id=sjces.device_id,
              punch_type="lunch_start", punched_at=_at(9), business_date=_DAY,
              timecard_id=card.timecard_id),
        Punch(employee_id=emp.employee_id, kiosk_device_id=sjces.device_id,
              punch_type="lunch_end", punched_at=_at(9, 30), business_date=_DAY,
              timecard_id=card.timecard_id),
    ])
    db_session.commit()

    hours = property_hours_for_timecard(db_session, card)
    # 12h gross - 0.5h lunch = 11.5h worked, split evenly on equal gross spans.
    assert sum(hours.values()) == Decimal("11.50")
    assert hours["SJCES"] == hours["58033"] == Decimal("5.75")


# --- the unrecorded bucket (pre-E1 adjustments) -----------------------------

def test_new_adjustments_carry_their_own_property(db_session):
    """Policy C: a correction records WHERE, so no estimate is needed."""
    emp, card, sjces, bw = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)
    db_session.add_all([
        TimecardAdjustment(
            timecard_id=card.timecard_id, punch_type="clock_in",
            adjusted_at=_at(13), business_date=_DAY, reason="missed punch",
            actor_subject="gm-1", property_id="58033",
        ),
        TimecardAdjustment(
            timecard_id=card.timecard_id, punch_type="clock_out",
            adjusted_at=_at(18), business_date=_DAY, reason="missed punch",
            actor_subject="gm-1", property_id="58033",
        ),
    ])
    db_session.commit()

    assert property_hours_for_timecard(db_session, card) == {
        "SJCES": Decimal("6.00"),
        "58033": Decimal("5.00"),
    }


def test_pre_e1_adjustment_minutes_spread_proportionally(db_session):
    """Policy A: a NULL-property correction splits along the day's device-derived
    ratio rather than landing wholesale on one hotel."""
    emp, card, sjces, bw = _seed(db_session)
    _span(db_session, card, emp, sjces, 6, 12)   # 6h SJCES
    _span(db_session, card, emp, bw, 12, 15)     # 3h 58033
    db_session.commit()

    resolved = property_minutes_for_day(
        db_session, employee_id=emp.employee_id, business_date=_DAY,
        property_minutes={"SJCES": 360, "58033": 180, None: 90},
    )
    assert None not in resolved
    assert sum(resolved.values()) == 630, "no minutes invented or lost"
    # 90 unrecorded split 2:1 -> 60/30
    assert resolved == {"SJCES": 420, "58033": 210}


def test_unrecorded_minutes_fall_back_to_primary_when_no_device_split(db_session):
    """A day reconstructed entirely by adjustment has no ratio to spread along."""
    emp, card, _sjces, _bw = _seed(db_session)
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="58033", is_primary=True,
        status="active", effective_from=_ANCHOR,
    ))
    db_session.commit()

    resolved = property_minutes_for_day(
        db_session, employee_id=emp.employee_id, business_date=_DAY,
        property_minutes={None: 480},
    )
    assert resolved == {"58033": 480}


def test_unattributable_hours_are_refused_not_dropped(db_session):
    """No device split AND no primary assignment. Dropping the hours understates
    a hotel's labor; parking them anywhere overstates another. Both silent."""
    emp, card, _sjces, _bw = _seed(db_session)
    db_session.commit()

    with pytest.raises(UnattributableHoursError, match="Refusing to guess"):
        property_minutes_for_day(
            db_session, employee_id=emp.employee_id, business_date=_DAY,
            property_minutes={None: 480},
        )


def test_split_always_sums_to_worked_minutes(db_session):
    """Largest-remainder rounding: a split that loses a minute produces a
    Schedule 14 that does not tie."""
    emp, card, sjces, bw = _seed(db_session)
    _span(db_session, card, emp, sjces, 9, 12)
    db_session.add_all([
        Punch(employee_id=emp.employee_id, kiosk_device_id=bw.device_id,
              punch_type="clock_in", punched_at=_at(13), business_date=_DAY,
              timecard_id=card.timecard_id),
        Punch(employee_id=emp.employee_id, kiosk_device_id=bw.device_id,
              punch_type="clock_out", punched_at=_at(16, 20), business_date=_DAY,
              timecard_id=card.timecard_id),
    ])
    db_session.add_all([
        Punch(employee_id=emp.employee_id, kiosk_device_id=sjces.device_id,
              punch_type="lunch_start", punched_at=_at(10), business_date=_DAY,
              timecard_id=card.timecard_id),
        Punch(employee_id=emp.employee_id, kiosk_device_id=sjces.device_id,
              punch_type="lunch_end", punched_at=_at(10, 37), business_date=_DAY,
              timecard_id=card.timecard_id),
    ])
    db_session.commit()

    from usali.timecards import compute_timecard
    day = next(d for d in compute_timecard(db_session, card) if d.worked_minutes > 0)
    assert sum(day.property_minutes.values()) == day.worked_minutes

    hours = property_hours_for_timecard(db_session, card)
    assert sum(hours.values()) == (
        Decimal(day.worked_minutes) / Decimal("60")
    ).quantize(Decimal("0.01"))
