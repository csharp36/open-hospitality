"""H4: the late-worked-minutes preflight guard (decision 4).

An UNLINKED punch is H1's marker: it landed in a period whose card was
already approved, so its minutes are in nobody's card and nobody's pay —
the §246(n) treatment, applied to worked time. The guard names each one
by employee and date; silence would leave the minutes unpaid forever.

The population is the PUNCHES themselves, never `paid_here` — the G7
Critical's lesson verbatim: a terminated employee is in no period's
population, and termination is exactly when a late correction lands
after payroll ran. Attribution is the punch's own (device → property),
not the primary placement: the minutes were clocked where they were
clocked.
"""

from datetime import UTC, date, datetime

from usali.models import (
    Department,
    KioskDevice,
    Organization,
    PaySchedule,
    Property,
    Punch,
)
from tests.employees import make_employee
from usali.payroll_run import assemble_pay_run_entries
from usali.timecards import assemble_timecard
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)  # period 2026-07-06 .. 2026-07-19

_GUARD_MARK = "linked to no timecard"


def _seed(db_session, *, second_property=False):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping")
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="adm")
    db_session.add_all([dept, device])
    db_session.add(PaySchedule(property_id="HISJ", frequency="biweekly",
                               anchor=_ANCHOR, check_date_offset_days=5))
    other_device = None
    if second_property:
        db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                                pms_source="OPERA", wage_jurisdiction="US-CA"))
        db_session.flush()
        other_device = KioskDevice(property_id="SSSJ", name="iPad2",
                                   token_hash="s" * 64, enrolled_by="adm")
        db_session.add(other_device)
    db_session.flush()
    return device, other_device


def _punch_day(db_session, device, emp_id, day, *, hours=(17, 23)):
    for ptype, hour in (("clock_in", hours[0]), ("clock_out", hours[1])):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device.device_id,
            punch_type=ptype,
            punched_at=datetime(day.year, day.month, day.day, hour,
                                tzinfo=UTC),
            business_date=day,
            photo_key=f"k/{emp_id}-{ptype}-{day.isoformat()}-{hour}",
        ))
    db_session.flush()


def _approved_card(db_session, emp_id, in_period):
    card = assemble_timecard(db_session, emp_id, in_period, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    return card


def _orphan(db_session, device, emp_id, day):
    """A punch landing AFTER its period's card was approved: recorded,
    assembled, and left unlinked — the H1 marker this guard names."""
    _punch_day(db_session, device, emp_id, day, hours=(9, 13))
    assemble_timecard(db_session, emp_id, day, anchor=_ANCHOR)
    db_session.commit()


def _guard_lines(report):
    return [p for p in report.problems if _GUARD_MARK in p]


def test_an_orphan_punch_blocks_the_run_by_name(db_session):
    """The blocker names WHO and WHEN, and says how to resolve (reopen —
    H3). Before the orphan lands, the same world raises no guard line: a
    linked punch is in its card and its pay."""
    device, _ = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _punch_day(db_session, device, emp.employee_id, date(2026, 7, 7))
    _approved_card(db_session, emp.employee_id, _PERIOD_DAY)

    clean = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                     anchor=_ANCHOR)
    assert _guard_lines(clean) == []

    _orphan(db_session, device, emp.employee_id, date(2026, 7, 8))
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    [line] = _guard_lines(report)
    assert f"employee {emp.employee_id} (Hank H)" in line
    assert "2026-07-08" in line
    assert "Reopen" in line
    assert not report.ok


def test_a_terminated_employees_orphan_still_blocks(db_session):
    """The G7 Critical's shape, for worked time: a terminated employee is
    in no period's `paid_here`, and termination is exactly when a late
    punch correction lands after payroll ran. The population is the
    punches, so they block anyway."""
    device, _ = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", full_name="Tara T",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _punch_day(db_session, device, emp.employee_id, date(2026, 7, 7))
    _approved_card(db_session, emp.employee_id, _PERIOD_DAY)
    _orphan(db_session, device, emp.employee_id, date(2026, 7, 9))

    emp.employment_status = "terminated"
    emp.termination_date = date(2026, 7, 20)
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    [line] = _guard_lines(report)
    assert f"employee {emp.employee_id} (Tara T)" in line
    assert "2026-07-09" in line


def test_an_earlier_periods_orphan_blocks_this_run_too(db_session):
    """An orphan from a PRIOR period is still unpaid minutes — this run
    cannot pay them either, but staying silent about them would leave
    them unpaid forever. Named until resolved, exactly like a late sick
    entry."""
    device, _ = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", full_name="Elia E",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    prior = date(2026, 6, 24)  # period 2026-06-22 .. 2026-07-05
    _punch_day(db_session, device, emp.employee_id, prior)
    _approved_card(db_session, emp.employee_id, prior)
    _orphan(db_session, device, emp.employee_id, date(2026, 6, 25))
    _punch_day(db_session, device, emp.employee_id, date(2026, 7, 7))
    _approved_card(db_session, emp.employee_id, _PERIOD_DAY)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    [line] = _guard_lines(report)
    assert "2026-06-25" in line


def test_another_propertys_orphan_does_not_block(db_session):
    """Attribution is the punch's own: clocked at SSSJ means SSSJ's run
    names it. Blocking HISJ's run for it would train people to ignore
    preflight (and HISJ cannot resolve it)."""
    device, other_device = _seed(db_session, second_property=True)
    hank = make_employee(db_session, property_id="HISJ", full_name="Hank H",
                         pay_type="hourly", pay_rate="20.00")
    sam = make_employee(db_session, property_id="SSSJ", full_name="Sam S",
                        pay_type="hourly", pay_rate="21.00")
    db_session.flush()
    _punch_day(db_session, device, hank.employee_id, date(2026, 7, 7))
    _approved_card(db_session, hank.employee_id, _PERIOD_DAY)
    _punch_day(db_session, other_device, sam.employee_id, date(2026, 7, 7))
    _approved_card(db_session, sam.employee_id, _PERIOD_DAY)
    _orphan(db_session, other_device, sam.employee_id, date(2026, 7, 8))

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert _guard_lines(report) == []


def test_a_later_periods_orphan_is_that_runs_problem(db_session):
    """Scope: this run names orphans dated through ITS period end. A later
    period's orphan is named by the run that should pay it — every orphan
    is some run's problem, no orphan is every run's problem."""
    device, _ = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _punch_day(db_session, device, emp.employee_id, date(2026, 7, 7))
    _approved_card(db_session, emp.employee_id, _PERIOD_DAY)
    later = date(2026, 7, 21)  # period 2026-07-20 .. 2026-08-02
    _punch_day(db_session, device, emp.employee_id, later)
    _approved_card(db_session, emp.employee_id, later)
    _orphan(db_session, device, emp.employee_id, date(2026, 7, 22))

    this_run = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                        anchor=_ANCHOR)
    assert _guard_lines(this_run) == []
    that_run = assemble_pay_run_entries(db_session, "HISJ", later,
                                        anchor=_ANCHOR)
    [line] = _guard_lines(that_run)
    assert "2026-07-22" in line


def test_reopening_and_relinking_clears_the_blocker(db_session):
    """The resolution loop closes: reopen relinks (H3), the punch is in a
    card again, and the guard falls silent — replaced by the not-approved
    blocker until the fresh review signs."""
    device, _ = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _punch_day(db_session, device, emp.employee_id, date(2026, 7, 7))
    card = _approved_card(db_session, emp.employee_id, _PERIOD_DAY)
    _orphan(db_session, device, emp.employee_id, date(2026, 7, 8))
    assert _guard_lines(assemble_pay_run_entries(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR
    ))

    card.status = "open"
    card.approved_by = None
    card.approved_at = None
    assemble_timecard(db_session, emp.employee_id, _PERIOD_DAY,
                      anchor=_ANCHOR)
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert _guard_lines(report) == []
    assert any("not approved" in p for p in report.problems)
