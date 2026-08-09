"""I1: the census becomes a SUBMIT fact; fetch only prices it.

`PayRunLineProperty` rows are written by `execute_pay_run` at submit
success — worked-hours split per property plus the line's sick credited
to the run's own property, money zeroed, department NULL — exactly the
`PayRunLine` pattern. `fetch_pay_run_results` prices the STORED rows
(money apportioned by stored hours, department resolved at fetch), so a
reopen + relink between submit and fetch can no longer move filed
dollars between hotels (the H9 deferral's reproduced drift: $53.33 of
paid gross migrating onto the other hotel's Schedule 14). A run
submitted before this pillar has no stored rows: its first fetch
derives the split live one last time and WRITES it — the
NULL-fingerprint posture, absence heals through the normal path.

Suppression stays fail-closed throughout: the gate counts PRICED people
(gross > 0), so submit-time zero-money rows are invisible to it.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from tests.test_g7_review_remediation import _execute
from tests.test_h5_stored_sick import _census, _dual_world
from usali.models import PayRunLine, PayRunLineProperty, Punch, Timecard
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import fetch_pay_run_results
from usali.timecards import assemble_timecard

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)


def _relink_late_sssj_punches(db_session, emp_id):
    """A 4h punch at SSSJ lands late, and the card is reopened so it
    RELINKS — the live weights now disagree with what the run paid."""
    device_id = db_session.execute(
        select(Punch.kiosk_device_id).where(
            Punch.employee_id == emp_id,
            Punch.business_date == date(2026, 7, 7),
        )
    ).scalars().first()
    for ptype, hour in (("clock_in", 18), ("clock_out", 22)):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(2026, 7, 9, hour, tzinfo=UTC),
            business_date=date(2026, 7, 9),
        ))
    db_session.flush()
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == emp_id,
                               Timecard.period_start == _PERIOD_DAY)
    ).scalar_one()
    card.status = "open"
    card.approved_by = None
    card.approved_at = None
    assemble_timecard(db_session, emp_id, _PERIOD_DAY, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()


def test_census_rows_land_at_submit_with_zero_money(db_session):
    """The split is frozen the moment the run submits: hours per property
    (worked where clocked, sick to the paying property), money zeroed
    until the provider prices it, department NULL until fetch resolves
    it. Zero gross keeps the rows invisible to the suppression gate —
    an unfetched run discloses nothing."""
    emp_id = _dual_world(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    rows = _census(db_session, run, emp_id)
    assert set(rows) == {"HISJ", "SSSJ"}
    assert Decimal(str(rows["HISJ"].hours)) == Decimal("16.00")
    assert Decimal(str(rows["SSSJ"].hours)) == Decimal("8.00")
    for row in rows.values():
        assert Decimal(str(row.gross)) == 0
        assert Decimal(str(row.employer_burden)) == 0
        assert row.department_id is None


def test_a_reopen_between_submit_and_fetch_moves_no_filed_dollars(db_session):
    """THE pin (the H9 deferral, reproduced there): the run paid 24h with
    a 16/8 HISJ/SSSJ split. A late 4h SSSJ punch relinks before fetch —
    the LIVE weights now say 16/12. The filed dollars must follow what
    was PAID, not what the card claims now: HISJ $320.00, SSSJ $160.00,
    summing to the line's $480.00. (The 4h delta is the worked-content
    guard's to name at the next preflight — not fetch's to book.)"""
    emp_id = _dual_world(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    _relink_late_sssj_punches(db_session, emp_id)

    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()

    rows = _census(db_session, run, emp_id)
    assert Decimal(str(rows["HISJ"].hours)) == Decimal("16.00")
    assert Decimal(str(rows["HISJ"].gross)) == Decimal("320.00")
    assert Decimal(str(rows["SSSJ"].hours)) == Decimal("8.00")
    assert Decimal(str(rows["SSSJ"].gross)) == Decimal("160.00")


def test_refetch_is_idempotent_and_never_duplicates(db_session):
    emp_id = _dual_world(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    first = {
        p: (Decimal(str(r.hours)), Decimal(str(r.gross)))
        for p, r in _census(db_session, run, emp_id).items()
    }
    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    again = {
        p: (Decimal(str(r.hours)), Decimal(str(r.gross)))
        for p, r in _census(db_session, run, emp_id).items()
    }
    assert first == again
    count = len(db_session.execute(
        select(PayRunLineProperty).where(
            PayRunLineProperty.pay_run_id == run.pay_run_id)
    ).scalars().all())
    assert count == 2


def test_a_pre_i_run_self_heals_at_first_fetch_then_stays(db_session):
    """A run submitted before this pillar has no stored split. Its first
    fetch derives live — one last time — and WRITES the rows; from then
    on the split is stored fact, and a later relink moves nothing."""
    emp_id = _dual_world(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    # Simulate pre-I: the submit-time rows never existed.
    db_session.execute(
        PayRunLineProperty.__table__.delete().where(
            PayRunLineProperty.pay_run_id == run.pay_run_id)
    )
    db_session.commit()

    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    rows = _census(db_session, run, emp_id)
    assert Decimal(str(rows["HISJ"].gross)) == Decimal("320.00")
    assert Decimal(str(rows["SSSJ"].gross)) == Decimal("160.00")

    # Healed means STORED: the same post-submit relink that used to move
    # money now moves nothing on a re-fetch.
    _relink_late_sssj_punches(db_session, emp_id)
    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    rows = _census(db_session, run, emp_id)
    assert Decimal(str(rows["HISJ"].gross)) == Decimal("320.00")
    assert Decimal(str(rows["SSSJ"].gross)) == Decimal("160.00")


def test_every_paid_line_has_census_rows_at_submit(db_session):
    """Population parity from the submit moment: the census and the paid
    population are the same set — including the sick-only entrant with
    no card at all (weights fall back to the paying property)."""
    from tests.employees import make_employee
    from tests.test_e5_provider_port import (
        _chain_row,
        _payable_employee,
        _ssn_profile,
    )
    from tests.test_payroll_run import _seed
    from tests.test_sick_pay_submission import _sick

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    solo = make_employee(db_session, property_id="HISJ",
                         department_id=dept_id, position_id=pos_id,
                         full_name="Sol O", pay_type="hourly",
                         pay_rate="22.00")
    db_session.flush()
    _ssn_profile(db_session, solo.employee_id)
    _chain_row(db_session, solo.employee_id, 1)
    db_session.commit()
    _sick(db_session, solo.employee_id, "8.00", date(2026, 7, 8))

    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    line_employees = set(db_session.execute(
        select(PayRunLine.employee_id).where(
            PayRunLine.pay_run_id == run.pay_run_id)
    ).scalars())
    census_employees = set(db_session.execute(
        select(PayRunLineProperty.employee_id).where(
            PayRunLineProperty.pay_run_id == run.pay_run_id)
    ).scalars())
    assert line_employees == census_employees == {hank, solo.employee_id}
    [solo_row] = [
        r for r in db_session.execute(
            select(PayRunLineProperty).where(
                PayRunLineProperty.pay_run_id == run.pay_run_id,
                PayRunLineProperty.employee_id == solo.employee_id)
        ).scalars()
    ]
    assert solo_row.property_id == "HISJ"
    assert Decimal(str(solo_row.hours)) == Decimal("8.00")
