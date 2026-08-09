"""E4 Task 3 + review remediation: accrual, the fold, recorded caps.

Post-review invariants, each pinned against a reproduced finding:
- caps are RECORDED per positive entry and the fold clamps at each day's own
  caps — today's day length can never restate history (statute Critical /
  money F3);
- the fold is DAY-granular, so a re-promote that rewrites entry ids cannot
  move a cap-adjacent balance (money F1);
- accrual quantizes ROUND_UP: "at least 1 per 30" is a floor (statute L2,
  and 16/30 -> 0.54 pins the direction — half-up would say 0.53);
- exempt deeming counts weeks EMPLOYED, not weeks punched (statute HIGH-1).
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee
from usali.labor import promote_timecard
from usali.models import (
    Department,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
    SickLeaveLedger,
)
from usali.sick_leave import balance_on, day_length, would_overdraw
from usali.timecards import assemble_timecard
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)  # Monday


def _seed(db_session, *, exempt=False):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Attendant",
                   flsa_exempt=exempt)
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="adm")
    db_session.add_all([pos, device])
    db_session.flush()
    return dept.department_id, pos.position_id, device.device_id


def _worker(db_session, dept_id, pos_id, *, name="Hank H", pay_type="hourly",
            rate="20.00"):
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name=name, pay_type=pay_type,
                        pay_rate=rate if pay_type == "hourly" else None)
    db_session.flush()
    return emp.employee_id


def _shift(db_session, device_id, emp_id, day, in_h, out_h, month=1):
    for ptype, h in (("clock_in", in_h), ("clock_out", out_h)):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(2026, month, day, h, tzinfo=UTC),
            business_date=date(2026, month, day),
            photo_key=f"k/{emp_id}-{ptype}{day}{h}",
        ))


def _approved_card(db_session, emp_id, period_day):
    card = assemble_timecard(db_session, emp_id, period_day, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    return card


def _entries(db_session, emp_id):
    """ORDERED list including row count — a set() here was the review's F1:
    duplicate rows collapse and a missing delete-before-rewrite survives."""
    return [
        (e.entry_type, e.hours, e.cap_hours, e.effective_on, e.timecard_id)
        for e in db_session.execute(
            select(SickLeaveLedger)
            .where(SickLeaveLedger.employee_id == emp_id)
            .order_by(SickLeaveLedger.effective_on, SickLeaveLedger.entry_id)
        ).scalars()
    ]


def _entry(db_session, emp_id, kind, hours, on, cap=None):
    db_session.add(SickLeaveLedger(
        employee_id=emp_id, entry_type=kind, hours=Decimal(hours),
        cap_hours=None if cap is None else Decimal(cap), effective_on=on,
    ))
    db_session.commit()


def test_promotion_accrues_one_thirtieth_rounded_up(db_session):
    """16h worked -> 16/30 = 0.5333 -> 0.54 CEILING. Half-up would say 0.53:
    this figure pins the direction, because 'at least one hour per 30' is a
    statutory floor and per-card half-up dipped a worst-case year below it.
    The entry records the cap in force on its date (8h days -> 80)."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    _shift(db_session, device_id, emp_id, 6, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    [(kind, hours, cap, effective, timecard_id)] = _entries(db_session, emp_id)
    assert (kind, hours, cap) == ("accrual", Decimal("0.54"), Decimal("80.00"))
    assert effective == card.period_end
    assert timecard_id == card.timecard_id


def test_overtime_hours_accrue_too(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 5, 8, 20)  # 12h
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert _entries(db_session, emp_id)[0][1] == Decimal("0.40")


def test_repromotion_reproduces_the_ordered_ledger_including_count(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    _shift(db_session, device_id, emp_id, 19, 9, 17)
    db_session.commit()
    card_a = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card_a, anchor=_ANCHOR)
    db_session.commit()
    card_b = _approved_card(db_session, emp_id, date(2026, 1, 19))
    promote_timecard(db_session, card_b, anchor=_ANCHOR)
    db_session.commit()
    before = _entries(db_session, emp_id)

    promote_timecard(db_session, card_a, anchor=_ANCHOR)
    db_session.commit()
    assert _entries(db_session, emp_id) == before  # ordered, counted


def test_repromote_near_the_cap_with_same_day_usage_moves_nothing(db_session):
    """The money review's F1, pinned: accrual and usage share Jan 18; the
    re-promote hands the accrual a NEW entry_id that sorts after the usage.
    The old per-entry fold answered 75.00 before and 75.67 after. The
    DAY-granular fold sums the day before clamping, so order inside a date
    cannot exist."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "78.00", date(2026, 1, 1),
           cap="80.00")
    _shift(db_session, device_id, emp_id, 5, 7, 17)  # 10h -> 0.34 accrual
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    _entry(db_session, emp_id, "usage", "-5.00", card.period_end)

    before = balance_on(db_session, emp_id, date(2026, 1, 31))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    after = balance_on(db_session, emp_id, date(2026, 1, 31))
    assert before == after


def test_exempt_staff_accrue_on_weeks_employed_not_weeks_punched(db_session):
    """Statute HIGH-1: the deeming is not conditioned on time capture. One
    punched day, TWO employed weeks -> 80/30 = 2.67 (ceiling)."""
    dept_id, pos_id, device_id = _seed(db_session, exempt=True)
    emp_id = _worker(db_session, dept_id, pos_id, pay_type="salary", rate=None)
    _shift(db_session, device_id, emp_id, 5, 9, 13)  # 4h, week 1 only
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert _entries(db_session, emp_id)[0][1] == Decimal("2.67")


def test_excluded_staff_accrue_nothing(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id,
                     pay_type="exclude_from_payroll", rate=None)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert _entries(db_session, emp_id) == []


def test_a_stale_accrual_is_deleted_even_when_the_card_goes_empty(db_session):
    """Money F9: the delete-before-rewrite must run BEFORE the empty-card
    early return. Driven directly at the unit, since real punches are
    immutable."""
    from usali.sick_leave import accrue_for_card

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert len(_entries(db_session, emp_id)) == 1

    accrue_for_card(db_session, card, day_hours={}, exempt=False,
                    jurisdiction="US-CA")
    db_session.commit()
    assert _entries(db_session, emp_id) == []


# --- the fold: recorded caps, day granularity, no retroactivity --------------


def test_balance_clamps_at_each_entrys_recorded_cap(db_session):
    dept_id, pos_id, _ = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "70.00", date(2026, 6, 1), cap="80.00")
    _entry(db_session, emp_id, "adjustment", "20.00", date(2026, 6, 2), cap="80.00")
    assert balance_on(db_session, emp_id, date(2026, 6, 30)) == Decimal("80.00")


def test_usage_frees_headroom_and_later_accrual_refills(db_session):
    dept_id, pos_id, _ = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "90.00", date(2026, 6, 1), cap="80.00")
    _entry(db_session, emp_id, "usage", "-10.00", date(2026, 6, 5))
    _entry(db_session, emp_id, "adjustment", "5.00", date(2026, 6, 10), cap="80.00")
    assert balance_on(db_session, emp_id, date(2026, 6, 30)) == Decimal("75.00")


def test_a_decaying_day_length_cannot_extinguish_banked_hours(db_session):
    """THE statute Critical, pinned: +100 lawfully banked under a 100-hour
    cap (D was 10), then -30 and +10 under today's smaller 80 cap. The old
    today's-D fold clamped Feb at 80 and answered 60 — an unlawful denial of
    20 accrued hours. With recorded caps: 100 -> 70 -> clamp(80) after +10
    -> 80. And nothing about the answer changes as more time passes with no
    entries (money F3)."""
    dept_id, pos_id, _ = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "100.00", date(2026, 2, 1),
           cap="100.00")
    _entry(db_session, emp_id, "usage", "-30.00", date(2026, 3, 1))
    _entry(db_session, emp_id, "adjustment", "10.00", date(2026, 4, 1),
           cap="80.00")
    assert balance_on(db_session, emp_id, date(2026, 5, 1)) == Decimal("80.00")
    assert balance_on(db_session, emp_id, date(2026, 12, 1)) == Decimal("80.00")


def test_a_capless_entry_folds_unclamped(db_session):
    """NULL cap_hours = no data or no mandate: the vouched figure stands.
    Flooring D to 8 with no data was statute HIGH-2 — it denied a 10h/day
    worker their lawful 50-hour floor at the pilot's own cutover."""
    dept_id, pos_id, _ = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "95.00", date(2026, 6, 1))
    assert balance_on(db_session, emp_id, date(2026, 6, 30)) == Decimal("95.00")


def test_a_usage_void_restores_the_balance(db_session):
    dept_id, pos_id, _ = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "20.00", date(2026, 6, 1), cap="80.00")
    _entry(db_session, emp_id, "usage", "-8.00", date(2026, 6, 5))
    _entry(db_session, emp_id, "usage_void", "8.00", date(2026, 6, 6))
    assert balance_on(db_session, emp_id, date(2026, 6, 30)) == Decimal("20.00")


def test_balance_is_as_of_a_date(db_session):
    dept_id, pos_id, _ = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "10.00", date(2026, 6, 1))
    _entry(db_session, emp_id, "adjustment", "10.00", date(2026, 7, 1))
    assert balance_on(db_session, emp_id, date(2026, 6, 15)) == Decimal("10.00")


def test_would_overdraw_sees_the_whole_timeline(db_session):
    """Money F2: +30, then 25 used mid-June. A 15-hour usage BACKDATED to
    June 2 passes a point-in-time check (balance was 30 that day) but drives
    June 15 to -10. The simulation folds the whole ledger and refuses."""
    dept_id, pos_id, _ = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _entry(db_session, emp_id, "adjustment", "30.00", date(2026, 6, 1), cap="80.00")
    _entry(db_session, emp_id, "usage", "-25.00", date(2026, 6, 15))
    assert would_overdraw(db_session, emp_id, Decimal("15.00"), date(2026, 6, 2))
    assert not would_overdraw(db_session, emp_id, Decimal("5.00"), date(2026, 6, 2))


def test_day_length_is_none_without_data_and_floors_at_eight_with_it(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    assert day_length(db_session, emp_id, date(2026, 1, 20)) is None
    _shift(db_session, device_id, emp_id, 5, 9, 13)  # 4h day
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert day_length(db_session, emp_id, date(2026, 1, 20)) == Decimal("8")


def test_day_length_tracks_long_days_and_raises_the_recorded_cap(db_session):
    """The DIR's 10-hour example end to end: D=10, so the accrual entry
    RECORDS cap 100, and the fold clamps a later windfall at 100, not 80."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 5, 7, 17)
    _shift(db_session, device_id, emp_id, 6, 7, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    assert day_length(db_session, emp_id, date(2026, 1, 20)) == Decimal("10")
    [(_, _, cap, _, _)] = _entries(db_session, emp_id)
    assert cap == Decimal("100.00")
    _entry(db_session, emp_id, "adjustment", "150.00", date(2026, 1, 21),
           cap="100.00")
    assert balance_on(db_session, emp_id, date(2026, 1, 22)) == Decimal("100.00")


def test_a_card_past_the_recheck_date_warns_naming_the_reference_doc(
    db_session, caplog
):
    """Statute M1: recheck_by was decorative. Wired to the CARD's date (not
    the wall clock), so it is deterministic: a card ending after 2027-01-01
    warns, naming the doc to re-verify."""
    import logging

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _worker(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 5, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp_id, date(2026, 1, 5))
    # A 2027-dated card via the unit (building 2027 punches needs no more).
    card.period_end = date(2027, 1, 17)
    from usali.sick_leave import accrue_for_card

    logging.getLogger("usali.sick_leave").disabled = False  # alembic fileConfig
    with caplog.at_level(logging.WARNING, logger="usali.sick_leave"):
        accrue_for_card(db_session, card, day_hours={date(2027, 1, 5): Decimal("8")},
                        exempt=False, jurisdiction="US-CA")
    assert any("re-check" in r.getMessage() and "ca-sick-leave" in r.getMessage()
               for r in caplog.records)
