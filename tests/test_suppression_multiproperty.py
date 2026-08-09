"""Suppression under multi-property attribution (E1 Task 8).

No production code — this task exists to prove the four suppression gates still
hold after Tasks 5 and 6 changed WHAT GETS WRITTEN into the snapshots they count.

The new shape they must defend against: one employee now emits labor facts in
TWO departments at TWO properties, and the overtime premium arising from their
combined day is shared between both. Three prior Criticals (B3, C3, D1) all came
from a suppression gate counting the wrong population, and D3 came from an
aggregate varying with a caller-controlled parameter. Both classes get a fresh
probe here against the shape that did not exist before.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from tests.employees import make_employee, set_rate_everywhere
from usali.labor import promote_timecard
from usali.models import (
    Department,
    EmployeeAssignment,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
    Timecard,
    UsaliLaborFact,
)
from usali.reporting import _labor_sections, summary_operating_statement

_ANCHOR = date(2026, 7, 6)  # Monday of the fixture's revenue date
_DAY = date(2026, 7, 7)


def _second_property(db_session):
    """A second property for the SHARED employee to also work at. It needs no
    revenue facts — the cross-property assertions here are made on labor facts,
    not on a second SOS."""
    org = db_session.execute(select(Organization)).scalars().first()
    db_session.add(Property(property_id="BW58", org_id=org.org_id, name="BW",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="BW58", name="Laundry",
                      usali_schedule_id=14, usali_edition=12)
    db_session.add(dept)
    db_session.flush()
    device = KioskDevice(property_id="BW58", name="iPad2", token_hash="z" * 64,
                         enrolled_by="a")
    db_session.add(device)
    db_session.flush()
    return dept, device


def _hisj_dept(db_session):
    dept = Department(property_id="HISJ", name="Housekeeping",
                      usali_schedule_id=14, usali_edition=12)
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Attendant", flsa_exempt=False)
    db_session.add(pos)
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="a")
    db_session.add_all([pos, device])
    db_session.flush()
    return dept, pos, device


def _employee(db_session, name, *, rate, placements):
    """placements: list of (property_id, department_id, position_id, is_primary)."""
    emp = make_employee(db_session, place_primary=False, property_id=placements[0][0], full_name=name,
                   pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    for property_id, department_id, position_id, primary in placements:
        db_session.add(EmployeeAssignment(
            employee_id=emp.employee_id, property_id=property_id,
            department_id=department_id, position_id=position_id,
            is_primary=primary, status="active", effective_from=_ANCHOR,
        ))
    db_session.flush()
    # One rate across every placement: these tests vary the POPULATION of a
    # department, not what anyone earns.
    set_rate_everywhere(db_session, emp, rate)
    return emp


def _punch(db_session, emp, device, start, end):
    for ptype, hour in (("clock_in", start), ("clock_out", end)):
        db_session.add(Punch(
            employee_id=emp.employee_id, kiosk_device_id=device.device_id,
            punch_type=ptype,
            punched_at=datetime(_DAY.year, _DAY.month, _DAY.day, hour, tzinfo=UTC),
            business_date=_DAY, photo_key=f"k/{emp.employee_id}/{ptype}/{start}",
        ))


def _promote(db_session, emp):
    card = Timecard(employee_id=emp.employee_id, period_start=_ANCHOR,
                    period_end=_ANCHOR + timedelta(days=13), status="approved",
                    approved_at=datetime.now(UTC))
    db_session.add(card)
    db_session.flush()
    for punch in db_session.execute(
        select(Punch).where(Punch.employee_id == emp.employee_id)
    ).scalars():
        punch.timecard_id = card.timecard_id
    db_session.flush()
    promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()
    return card


def _housekeeping(sos):
    return next(line for line in sos.payroll_expense if line.department == "Housekeeping")


def _facts(db_session):
    return {
        (f.property_id, f.department_id): f
        for f in db_session.execute(select(UsaliLaborFact)).scalars()
    }


# --- the population must not be inflated by the split ------------------------

def test_a_shared_employee_alone_in_a_department_still_suppresses(db_session, seed_six_pdfs):
    """THE regression this task is for. Vikram now emits facts at BOTH
    properties. If the split were miscounted as two contributors, a solo
    department would stop suppressing and leak his rate.
    """
    hisj_dept, pos, hisj_device = _hisj_dept(db_session)
    bw_dept, bw_device = _second_property(db_session)
    vikram = _employee(db_session, "Vikram Jindal", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
        ("BW58", bw_dept.department_id, None, False),
    ])
    _punch(db_session, vikram, hisj_device, 6, 12)   # 6h HISJ
    _punch(db_session, vikram, bw_device, 13, 18)    # 5h BW58
    db_session.commit()
    _promote(db_session, vikram)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)

    hk = _housekeeping(sos)
    assert hk.est_cost is None, "a solo department must suppress even when split"
    assert sos.labor_suppressed_departments == 1
    # Hours are operational, not the rate, and still show -- but only HISJ's.
    assert hk.hours == Decimal("6.00")


def test_the_suppressed_cost_appears_in_no_total(db_session, seed_six_pdfs):
    """Complementary suppression: excluded from the line AND the total, so
    subtraction cannot recover it."""
    hisj_dept, pos, hisj_device = _hisj_dept(db_session)
    bw_dept, bw_device = _second_property(db_session)
    vikram = _employee(db_session, "Vikram Jindal", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
        ("BW58", bw_dept.department_id, None, False),
    ])
    _punch(db_session, vikram, hisj_device, 6, 12)
    _punch(db_session, vikram, bw_device, 13, 18)
    db_session.commit()
    _promote(db_session, vikram)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)
    assert sos.payroll_expense_total == Decimal("0")


def test_each_property_counts_its_own_population_independently(db_session, seed_six_pdfs):
    """Vikram is solo at BOTH properties. Neither may disclose, and the fact
    that he appears twice GLOBALLY must not read as a population of two
    ANYWHERE. This is the B3/C3/D1 failure class in its new shape."""
    hisj_dept, pos, hisj_device = _hisj_dept(db_session)
    bw_dept, bw_device = _second_property(db_session)
    vikram = _employee(db_session, "Vikram Jindal", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
        ("BW58", bw_dept.department_id, None, False),
    ])
    _punch(db_session, vikram, hisj_device, 6, 12)
    _punch(db_session, vikram, bw_device, 13, 18)
    db_session.commit()
    _promote(db_session, vikram)

    facts = _facts(db_session)
    # Facts exist at both properties...
    assert ("HISJ", hisj_dept.department_id) in facts
    assert ("BW58", bw_dept.department_id) in facts
    # ...each sourced by exactly ONE distinct employee.
    for (property_id, _department_id), fact in facts.items():
        card = db_session.get(Timecard, fact.timecard_id)
        assert card.employee_id == vikram.employee_id

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)
    assert _housekeeping(sos).est_cost is None


# --- a real department must still disclose, and only its own share -----------

def test_a_two_employee_department_discloses_only_its_own_share(db_session, seed_six_pdfs):
    """Not everything suppresses. A genuine two-employee department shows cost --
    and shows the HISJ SHARE of the shared employee, not their whole day.
    Carrying the full cost would both overstate HISJ and disclose more of the
    shared employee than HISJ actually sourced.
    """
    hisj_dept, pos, hisj_device = _hisj_dept(db_session)
    bw_dept, bw_device = _second_property(db_session)

    vikram = _employee(db_session, "Vikram Jindal", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
        ("BW58", bw_dept.department_id, None, False),
    ])
    _punch(db_session, vikram, hisj_device, 6, 12)   # 6h HISJ
    _punch(db_session, vikram, bw_device, 13, 18)    # 5h BW58
    db_session.commit()
    _promote(db_session, vikram)

    alice = _employee(db_session, "Alice A", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
    ])
    _punch(db_session, alice, hisj_device, 9, 17)    # 8h HISJ
    db_session.commit()
    _promote(db_session, alice)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)
    hk = _housekeeping(sos)

    assert hk.est_cost is not None, "two distinct priced employees -- no suppression"
    # HISJ sees 6h of Vikram + 8h of Alice, NOT Vikram's full 11-hour day.
    assert hk.hours == Decimal("14.00")


# --- the D3 class: differencing across the new surface -----------------------

def test_cross_property_totals_do_not_isolate_a_shared_employee(db_session, seed_six_pdfs):
    """The D3 lesson generalised to the surface Task 6 created.

    An actor reading BOTH properties must not be able to subtract one from the
    other to isolate the shared employee. Because suppression is complementary,
    a suppressed department contributes to NEITHER its line nor its total -- so
    there is no residual to difference against.
    """
    hisj_dept, pos, hisj_device = _hisj_dept(db_session)
    bw_dept, bw_device = _second_property(db_session)

    vikram = _employee(db_session, "Vikram Jindal", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
        ("BW58", bw_dept.department_id, None, False),
    ])
    _punch(db_session, vikram, hisj_device, 6, 12)
    _punch(db_session, vikram, bw_device, 13, 18)
    db_session.commit()
    _promote(db_session, vikram)

    alice = _employee(db_session, "Alice A", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
    ])
    _punch(db_session, alice, hisj_device, 9, 17)
    db_session.commit()
    _promote(db_session, alice)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)
    hk = _housekeeping(sos)
    facts = _facts(db_session)

    # HISJ discloses a TWO-person aggregate, so it shows.
    assert hk.est_cost is not None, (
        "two priced employees at HISJ Housekeeping -- withholding here would be "
        "over-suppression, and the disclose side needs a fence too"
    )

    # BW58's Laundry holds Vikram ALONE, so its cost must be withheld on
    # BW58's own report. This is the assertion the test was named for and did
    # not make: it previously counted employees over the fixture's own fact
    # rows, which is a property of the fixture, never calling
    # summary_operating_statement for BW58 at all. Every mutation survived it,
    # including disabling suppression outright.
    # `_labor_sections` rather than the full SOS: BW58 has no REVENUE facts, so
    # `summary_operating_statement` refuses outright -- which is exactly how the
    # original test ended up asserting nothing about the gate. This is the gate.
    bw_lines, bw_cost_total, *_rest = _labor_sections(
        db_session, "BW58", _DAY, _DAY
    )
    bw_hk = next(ln for ln in bw_lines if ln.department == "Laundry")
    assert bw_hk.est_cost is None, (
        "Vikram is alone in BW58 Laundry; disclosing its cost publishes his "
        "share directly, and cost / hours re-derives his rate"
    )

    # THE DIFFERENCING ATTACK, stated as arithmetic rather than as two numbers
    # that happen to differ. An actor reading BOTH reports has HISJ's two-person
    # aggregate and nothing from BW58 -- so Vikram-at-HISJ cannot be recovered by
    # subtracting anything BW58 published, because BW58 published nothing.
    facts = _facts(db_session)
    bw_share = facts[("BW58", bw_dept.department_id)].est_cost
    assert bw_share > 0, "the fact row exists for the ledger; only the REPORT hides it"
    # Complementary: the withheld share reaches no total either, or subtracting
    # the shown lines from it would recover the hidden one.
    assert bw_cost_total == Decimal("0.00")
    # Hours still show -- they are operational. Safe ONLY while cost is withheld.
    assert bw_hk.hours > 0


def test_hours_visible_with_cost_suppressed_does_not_yield_a_rate(db_session, seed_six_pdfs):
    """Hours are deliberately NOT suppressed (they are operational). That is only
    safe while cost is withheld -- rate = cost / hours is the whole attack."""
    hisj_dept, pos, hisj_device = _hisj_dept(db_session)
    solo = _employee(db_session, "Solo S", rate="37.50", placements=[
        ("HISJ", hisj_dept.department_id, pos.position_id, True),
    ])
    _punch(db_session, solo, hisj_device, 9, 17)
    db_session.commit()
    _promote(db_session, solo)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)
    hk = _housekeeping(sos)

    assert hk.hours == Decimal("8.00")
    assert hk.est_cost is None
    assert sos.payroll_expense_total == Decimal("0")
    # 8h x 37.50 = 300.00 must appear nowhere on the report.
    assert Decimal("300.00") not in {
        line.est_cost for line in sos.payroll_expense if line.est_cost is not None
    }


# --- exempt status is part of the PRICED population --------------------------

def test_mixed_exempt_and_nonexempt_positions_resolve_to_non_exempt(db_session, seed_six_pdfs):
    """A judgment call, tested because it decides the priced population.

    Someone holding an EXEMPT position at one property and a NON-EXEMPT one at
    the other is treated as non-exempt. Exempt staff are never hourly-costed, so
    taking the exempt answer would omit real hourly cost from a property's
    Schedule 14. It is also the legally coherent reading: FLSA exemption turns on
    duties and salary basis for the workweek, and performing non-exempt work
    generally defeats it.
    """
    from usali.assignments import is_exempt_on

    hisj_dept, hourly_pos, hisj_device = _hisj_dept(db_session)
    bw_dept, _bw_device = _second_property(db_session)
    exempt_pos = Position(department_id=bw_dept.department_id, title="GM", flsa_exempt=True)
    db_session.add(exempt_pos)
    db_session.flush()

    mixed = _employee(db_session, "Mixed M", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, hourly_pos.position_id, True),
        ("BW58", bw_dept.department_id, exempt_pos.position_id, False),
    ])
    db_session.commit()

    assert is_exempt_on(db_session, mixed.employee_id, _DAY) is False


def test_all_exempt_positions_resolve_to_exempt(db_session, seed_six_pdfs):
    from usali.assignments import is_exempt_on

    hisj_dept, _hourly_pos, _device = _hisj_dept(db_session)
    bw_dept, _bw_device = _second_property(db_session)
    gm_hisj = Position(department_id=hisj_dept.department_id, title="GM", flsa_exempt=True)
    gm_bw = Position(department_id=bw_dept.department_id, title="GM", flsa_exempt=True)
    db_session.add_all([gm_hisj, gm_bw])
    db_session.flush()

    gm = _employee(db_session, "Mihirkumar D", rate="0.00", placements=[
        ("HISJ", hisj_dept.department_id, gm_hisj.position_id, True),
        ("BW58", bw_dept.department_id, gm_bw.position_id, False),
    ])
    db_session.commit()

    assert is_exempt_on(db_session, gm.employee_id, _DAY) is True


def test_an_exempt_employee_does_not_count_toward_the_priced_population(
    db_session, seed_six_pdfs
):
    """The D1 Critical in its new shape: an exempt manager plus ONE hourly
    worker is a priced population of ONE, and must suppress. Counting assigned
    rather than priced employees is exactly what leaked the rate before."""
    hisj_dept, hourly_pos, hisj_device = _hisj_dept(db_session)
    gm_pos = Position(department_id=hisj_dept.department_id, title="GM", flsa_exempt=True)
    db_session.add(gm_pos)
    db_session.flush()

    gm = _employee(db_session, "Exempt GM", rate="60.00", placements=[
        ("HISJ", hisj_dept.department_id, gm_pos.position_id, True),
    ])
    _punch(db_session, gm, hisj_device, 8, 18)
    db_session.commit()
    _promote(db_session, gm)

    hourly = _employee(db_session, "One Hourly", rate="20.00", placements=[
        ("HISJ", hisj_dept.department_id, hourly_pos.position_id, True),
    ])
    _punch(db_session, hourly, hisj_device, 9, 17)
    db_session.commit()
    _promote(db_session, hourly)

    sos = summary_operating_statement(db_session, property_id="HISJ", business_date=_DAY)
    hk = _housekeeping(sos)

    assert hk.est_cost is None, (
        "two ASSIGNED employees but only one PRICED -- must suppress"
    )
    assert sos.payroll_expense_total == Decimal("0")
