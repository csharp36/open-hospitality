"""E4 Task 5: sick pay on the SOS — priced like wages, suppressed like wages.

A usage entry stores HOURS and a date; dollars are DERIVED at report time
(regular rate on the day taken, primary placement), so a re-run reproduces a
closed window byte for byte. Disclosure: one person's sick cost / their sick
hours IS their rate, so department sick cost obeys the same per-unit
`_discloses` rule as wages — every business date carrying sick money needs
≥ 2 distinct priced sick-paid employees, or the department's figure hides
and stays out of every total.
"""

from datetime import date
from decimal import Decimal


from tests.test_e2_suppression_gates import _housekeeping, _worker
from usali.models import SickLeaveLedger
from usali.reporting import summary_operating_statement

_WORKED = date(2026, 7, 7)


def _sick(db_session, emp, hours, on):
    db_session.add(SickLeaveLedger(
        employee_id=emp.employee_id, entry_type="usage",
        hours=Decimal(hours) * -1, effective_on=on,
    ))
    db_session.commit()


def _sos(db_session, frm=_WORKED, to=None):
    if to is None:
        return summary_operating_statement(
            db_session, property_id="HISJ", business_date=frm
        )
    return summary_operating_statement(
        db_session, property_id="HISJ", date_from=frm, date_to=to
    )


def _line(sos, name="Housekeeping"):
    return next(ln for ln in sos.sick_pay if ln.department == name)


def test_two_sick_paid_people_disclose_summed_cost(db_session, seed_six_pdfs):
    """8h at $20 + 4h at $25 = $260, hours 12. Cost derives from the dated
    rates on the day taken — nothing is stored."""
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    _sick(db_session, a, "8.00", _WORKED)
    _sick(db_session, b, "4.00", _WORKED)

    sos = _sos(db_session)
    line = _line(sos)
    assert line.hours == Decimal("12.00")
    assert line.cost == Decimal("260.00")
    assert sos.sick_pay_total == Decimal("260.00")


def test_one_sick_paid_person_is_suppressed_and_in_no_total(
    db_session, seed_six_pdfs
):
    """cost / hours = the person's exact rate. Hidden, complementary."""
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="23.17")
    _worker(db_session, dept, pos, device, "Bea B", rate="25.00")  # no sick
    _sick(db_session, a, "8.00", _WORKED)

    sos = _sos(db_session)
    line = _line(sos)
    assert line.cost is None, "one person's sick cost IS their rate"
    assert line.hours == Decimal("8.00")  # operational, still shown
    assert sos.sick_pay_total == Decimal("0.00")
    assert sos.sick_suppressed_departments == 1


def test_solo_days_cannot_be_differenced_even_when_the_window_has_two(
    db_session, seed_six_pdfs
):
    """The differencing class, applied on day one of the new surface: Ada
    sick on the 7th, Bea on the 8th — the window holds two people, but each
    DAY is solitary, and two windows would subtract to a single person's
    rate. Per-unit `_discloses`: suppressed."""
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    _sick(db_session, a, "8.00", date(2026, 7, 7))
    _sick(db_session, b, "8.00", date(2026, 7, 8))

    sos = _sos(db_session, frm=date(2026, 7, 7), to=date(2026, 7, 8))
    assert _line(sos).cost is None
    assert sos.sick_pay_total == Decimal("0.00")


def test_no_rate_on_the_day_taken_is_unpriced_never_zero(
    db_session, seed_six_pdfs
):
    """A rate that starts AFTER the sick day: the hours are real, the cost is
    underivable — named in sick_unpriced_hours, and the unpriced person does
    not count toward the disclosure population."""
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00",
                rate_from=date(2026, 7, 9))  # in force after the sick day
    _sick(db_session, a, "8.00", _WORKED)
    _sick(db_session, b, "4.00", _WORKED)

    sos = _sos(db_session)
    line = _line(sos)
    assert line.hours == Decimal("12.00")
    assert line.cost is None, (
        "with Bea unpriced, Ada is the only PRICED sick person — suppressed"
    )
    assert sos.sick_unpriced_hours == Decimal("4.00")


def test_usage_outside_the_window_and_other_properties_stay_out(
    db_session, seed_six_pdfs
):
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    _sick(db_session, a, "8.00", date(2026, 6, 1))   # before the window
    _sick(db_session, b, "8.00", date(2026, 6, 1))

    sos = _sos(db_session)
    assert sos.sick_pay == []
    assert sos.sick_pay_total == Decimal("0.00")


def test_the_wage_section_is_untouched_by_sick_entries(db_session, seed_six_pdfs):
    """Sick pay is a SEPARATE section: usage entries must not perturb the
    Schedule 14/15 wage lines the E2/E3 gates protect."""
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00",
                days=(_WORKED,))
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00",
                days=(_WORKED,))
    before = _sos(db_session)
    wage_line = next(
        ln for ln in before.payroll_expense if ln.department == "Housekeeping"
    )
    _sick(db_session, a, "8.00", _WORKED)
    _sick(db_session, b, "8.00", _WORKED)
    after = _sos(db_session)
    wage_after = next(
        ln for ln in after.payroll_expense if ln.department == "Housekeeping"
    )
    assert (wage_line.hours, wage_line.est_cost) == (
        wage_after.hours, wage_after.est_cost
    )


def test_another_propertys_sick_entries_stay_out(db_session, seed_six_pdfs):
    """Review F3: the old cross-property test never BUILT another property,
    so removing the property filter survived every suite. Cyd's primary
    placement is at SSSJ: her entry must appear in no HISJ line, total, or
    unpriced figure."""
    from tests.employees import make_employee

    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    cyd = make_employee(db_session, property_id="SSSJ", full_name="Cyd C",
                        pay_type="hourly", pay_rate="30.00")
    db_session.commit()
    _sick(db_session, a, "8.00", _WORKED)
    _sick(db_session, b, "4.00", _WORKED)
    _sick(db_session, cyd, "8.00", _WORKED)

    sos = _sos(db_session)
    line = _line(sos)
    assert line.hours == Decimal("12.00"), "Cyd's 8h must not leak into HISJ"
    assert line.cost == Decimal("260.00")
    assert sos.sick_unpriced_hours == Decimal("0.00")
    # G7 (tests S12): the three asserts above all passed with the property
    # filter DELETED — Cyd's entry leaked into a solo phantom department
    # line, suppressed, invisible to every figure asserted. Pin the SHAPE:
    # exactly the one Housekeeping line, and nothing suppressed.
    assert len(sos.sick_pay) == 1
    assert sos.sick_suppressed_departments == 0


def test_an_unattributable_entry_is_warned_not_swallowed(
    db_session, seed_six_pdfs, caplog
):
    """Money F4 residue: reachable only by out-of-band writes now (the API
    refuses placement-less usage), but silence would under-report a money
    section — so it is NAMED server-side, ids only."""
    import logging

    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    _sick(db_session, a, "8.00", _WORKED)
    _sick(db_session, b, "4.00", _WORKED)
    _sick(db_session, a, "8.00", date(2020, 1, 1))  # pre-placement: orphan

    # alembic's fileConfig (run by the conftest migrations) disables
    # already-created loggers; re-enable before capturing.
    logging.getLogger("usali.reporting").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.reporting"):
        sos = _sos(db_session, frm=date(2020, 1, 1), to=_WORKED)
    assert any("no resolvable primary placement" in r.getMessage()
               for r in caplog.records)
    assert _line(sos).hours == Decimal("12.00")


def test_the_three_surfaces_agree_end_to_end(db_session, seed_six_pdfs):
    """Review F7: promotion, the API, and the SOS only met in production.
    One flow: promote real cards (accrual), record usage through the REAL
    routes for BOTH workers, then read the SOS and the balance — signs and
    magnitudes must agree across all three."""
    from fastapi.testclient import TestClient

    from tests.authkit import make_authkit
    from usali.db import make_session_factory
    from usali.keycloak_admin import InMemoryKeycloakAdmin
    from usali.server import create_app
    from usali.sick_leave import balance_on

    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    # promotion accrued 8h/30 = 0.27 each; give them usable banks too
    for emp in (a, b):
        db_session.add(SickLeaveLedger(
            employee_id=emp.employee_id, entry_type="adjustment",
            hours=Decimal("20.00"), cap_hours=Decimal("80.00"),
            effective_on=date(2026, 7, 1),
        ))
    db_session.commit()


    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=None, processed_dir=None, failed_dir=None,
        session_factory=make_session_factory(db_session.get_bind()),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    c = TestClient(app)
    from tests.grants import grant_role

    grant_role(db_session, "payroll_admin", sub="pa")  # L4: DB-backed authority
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    for emp, hours in ((a, "8.00"), (b, "4.00")):
        r = c.post(
            f"/api/payroll/employees/{emp.employee_id}/sick-leave/usage",
            json={"hours": hours, "on": "2026-07-08"}, headers=pa,
        )
        assert r.status_code == 200, r.text

    db_session.expire_all()
    sos = _sos(db_session, frm=_WORKED, to=date(2026, 7, 8))
    line = _line(sos)
    assert line.hours == Decimal("12.00")
    assert line.cost == Decimal("260.00")  # 8x20 + 4x25
    # accrued 0.27 (promotion) + 20.00 (adjustment) - 8.00 (usage) = 12.27
    assert balance_on(db_session, a.employee_id, date(2026, 7, 31)) == Decimal("12.27")


def test_full_sick_section_regression_pin_for_the_g3_hoist(
    db_session, seed_six_pdfs
):
    """The G3 hoist moves this derivation into `sick_days_taken` (shared
    with the pay-run submission). For a void-free window the output must
    stay BYTE-IDENTICAL — this pins the whole section payload at once:
    hours, cost, unpriced, and total, with a priced pair, an unpriced
    person, and an out-of-window entry all present."""
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    c = _worker(db_session, dept, pos, device, "Cal C", rate="30.00",
                rate_from=date(2026, 7, 9))  # unpriced on the sick day
    _sick(db_session, a, "8.00", _WORKED)
    _sick(db_session, b, "4.00", _WORKED)
    _sick(db_session, c, "2.00", _WORKED)
    _sick(db_session, a, "1.00", date(2026, 6, 1))  # outside the window

    sos = _sos(db_session)
    line = _line(sos)
    assert line.hours == Decimal("14.00")
    assert line.cost == Decimal("260.00")  # Ada+Bea priced pair; Cal unpriced
    assert sos.sick_unpriced_hours == Decimal("2.00")
    assert sos.sick_pay_total == Decimal("260.00")


def test_a_voided_usage_leaves_the_sos(db_session, seed_six_pdfs):
    """G3 (deliberate change, found grounding the submission): the cap fold
    and `_used_in_year` both treat a voided usage as never taken, but the
    SOS was still deriving COST from it — phantom benefits dollars on a
    disclosure surface, and the submission would have PAID them. Voids now
    net per employee-day in the one shared derivation, so both surfaces
    drop them together. A void exceeding the day's usage clamps at zero —
    it can reduce hours taken to nothing, never below."""
    dept, pos, device = _housekeeping(db_session)
    a = _worker(db_session, dept, pos, device, "Ada A", rate="20.00")
    b = _worker(db_session, dept, pos, device, "Bea B", rate="25.00")
    v = _worker(db_session, dept, pos, device, "Vic V", rate="30.00")
    _sick(db_session, a, "8.00", _WORKED)
    _sick(db_session, b, "4.00", _WORKED)
    _sick(db_session, v, "6.00", _WORKED)
    db_session.add(SickLeaveLedger(
        employee_id=v.employee_id, entry_type="usage_void",
        hours=Decimal("6.00"), effective_on=_WORKED,
    ))
    db_session.commit()

    sos = _sos(db_session)
    line = _line(sos)
    assert line.hours == Decimal("12.00"), "Vic's voided day is not sick pay"
    assert line.cost == Decimal("260.00")
    assert sos.sick_pay_total == Decimal("260.00")
