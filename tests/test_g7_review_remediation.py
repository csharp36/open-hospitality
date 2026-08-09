"""G7 adversarial-review remediation — money lens findings A, B, C.

A (Critical): the §246(n) late-sick guard drew its population from THIS
period's `paid_here`, so a terminated (or transferred) employee's
late-recorded usage was never selected — silently unpaid forever, for
exactly the population most likely to have paperwork land after payroll.
The guard now draws its population from the LEDGER.

B (High): a usage entry recorded between preflight's derivation and the
provider submission carried `created_at < submitted_at`, so every later
preflight concluded "that run paid it" — wrong, the submitted gross
provably excluded it. `execute_pay_run` now re-assembles right before
submit and fails the run LOUDLY on any drift (nothing sent, replaceable
like any failed run). The residual window is the submit HTTP call itself
— recorded in the backlog beside the transaction-timestamp race family.

C (Medium): sick usage dated on a day the employee also worked STACKED
silently — 8h worked + 8h sick on one date paid 16 hours. Partial days
must stack (worked 4h, went home sick 4h); a stack exceeding the
employee's day length is almost surely a misdated entry and now refuses
by name.
"""

import time
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tests.test_e5_provider_port import (
    _OPENER,
    _chain_row,
    _payable_employee,
)
from tests.test_payroll_run import _approved_card, _seed, _shift
from tests.test_sick_pay_submission import _assemble, _sick
from usali.models import EmployeeAssignment
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import assemble_pay_run_entries, execute_pay_run

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)   # period 2026-07-06 .. 2026-07-19
_SICK_DAY = date(2026, 7, 8)
_NEXT_PERIOD_DAY = date(2026, 7, 20)


def _execute(db_session, provider):
    run = execute_pay_run(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR, provider=provider,
        provider_name="memory", opener=_OPENER, actor="test",
    )
    db_session.commit()
    return run


# --- Finding A: the late-sick guard's population ------------------------------


def test_late_sick_of_a_terminated_employee_still_blocks(db_session):
    """Hank works period 1, the run submits, Hank terminates at the period
    boundary — and THEN HR records his sick day (the normal order: the
    paperwork lands after payroll ran). He is no longer in any `paid_here`,
    but §246(n) doesn't stop applying at termination: the next preflight
    must name him, not go green."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    wanda = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, wanda, 1)
    provider = InMemoryPayrollProvider()
    _execute(db_session, provider)
    time.sleep(0.05)

    # Terminate Hank: [effective_from, effective_to) — 07-20 ends coverage
    # after period 1's last day, so period 2's population excludes him.
    assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == hank)
    ).scalar_one()
    assignment.effective_to = date(2026, 7, 20)
    db_session.commit()
    _sick(db_session, hank, "8.00", _SICK_DAY)  # dated in the PAID period

    # Next period: Wanda works, so the run is alive without Hank.
    _shift(db_session, device_id, wanda, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, wanda, period_day=_NEXT_PERIOD_DAY)

    report = assemble_pay_run_entries(
        db_session, "HISJ", _NEXT_PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "sick" in named[0] and "2026-07-08" in named[0]


# --- Finding B: drift between preflight and submission ------------------------


def test_a_ledger_write_during_submission_fails_the_run_loudly(db_session):
    """A sick entry recorded after preflight assembled but before the
    provider call: the run's entries provably exclude it. Silence would
    let the late-sick guard call it paid (created_at < submitted_at).
    The run must fail LOUDLY before anything is sent — replaceable like
    any failed run, and the replacement pays the hours."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()

    fired = []
    real_sync = provider.sync_employee

    def sync_and_record(employee):
        pid = real_sync(employee)
        if not fired:  # once — the replacement run must not re-drift
            fired.append(True)
            _sick(db_session, hank, "8.00", _SICK_DAY)
        return pid

    provider.sync_employee = sync_and_record  # type: ignore[method-assign]

    run = _execute(db_session, provider)
    assert run.status == "failed"
    assert run.failure_reason is not None and "drift" in run.failure_reason
    assert provider._runs == {}  # nothing reached the provider

    # The failed run is replaceable, and the replacement pays the sick day.
    run2 = _execute(db_session, provider)
    assert run2.status == "submitted"
    result = provider.get_pay_run(run2.provider_run_id)
    # 8h worked + 8h sick at $20 — the drifted-in hours are IN the money.
    assert result.lines[0].gross == Decimal("320.00")


# --- Finding C: sick stacked on a worked day ----------------------------------


def test_sick_stacked_on_a_full_worked_day_blocks_by_name(db_session):
    """Hank worked his full 8h on 07-06 AND carries 8h of sick usage dated
    the same day: 16 paid hours for one day, almost surely a misdated
    entry. Refuse by name rather than move double money silently."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "8.00", date(2026, 7, 6))  # his worked day

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "2026-07-06" in named[0]
    assert "sick" in named[0] and "worked" in named[0]


def test_a_partial_sick_day_still_stacks_and_pays(db_session):
    """Worked 4h, went home sick 4h — the legitimate stack. 4 + 4 = 8 fits
    the day length, so it pays: regular 4.00 AND sick 4.00 on one entry."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    # Replace his worked day with a half day: second shift 07-07 stays
    # absent; his seeded 07-06 shift is 8h — use a NEW employee instead.
    from tests.employees import make_employee

    from usali.models import Punch  # noqa: F401  (imported for clarity)

    half = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Hal F",
                         pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    from tests.test_e5_provider_port import _ssn_profile

    _ssn_profile(db_session, half.employee_id)
    _chain_row(db_session, half.employee_id, 1)
    _shift(db_session, device_id, half.employee_id, 7, 8, 12)  # 4h on 07-07
    db_session.commit()
    _approved_card(db_session, half.employee_id)
    _sick(db_session, half.employee_id, "4.00", date(2026, 7, 7))

    report = _assemble(db_session)
    assert report.ok, report.problems
    entry = next(e for e in report.entries
                 if e.employee_id == half.employee_id)
    assert entry.regular_hours == Decimal("4.00")
    assert entry.sick_hours == Decimal("4.00")


# --- Tests-lens survivors S1-S10: each test kills a mutant that survived ------


def test_an_unattributable_sick_day_blocks_the_submission_by_name(db_session):
    """S1: the submission-path 'no resolvable primary placement' refusal
    was pinned only on the REPORTING copy. Hank's assignment ends 07-08
    ([from, to) — exclusive), the usage is dated 07-10: no placement, no
    property's run to pay it — the run must name him, not go green."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == hank)
    ).scalar_one()
    assignment.effective_to = date(2026, 7, 8)
    db_session.commit()
    _sick(db_session, hank, "8.00", date(2026, 7, 10))  # no placement

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "no resolvable primary placement" in named[0]
    assert "2026-07-10" in named[0]


def test_multi_day_sick_sums_every_day(db_session):
    """S2: no test submitted more than one sick day per employee — a
    mutant paying only the FIRST day survived. Two 8h days must pay 16."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "8.00", date(2026, 7, 8))
    _sick(db_session, hank, "8.00", date(2026, 7, 9))

    report = _assemble(db_session)
    assert report.ok, report.problems
    entry = next(e for e in report.entries if e.employee_id == hank)
    assert entry.sick_hours == Decimal("16.00")


def _second_property(db_session):
    from usali.models import Department, Property

    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="SSSJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    return dept.department_id


def test_another_propertys_sick_day_is_not_paid_here(db_session):
    """S3: no submission test built cross-property sick. Hank's primary was
    SSSJ through 07-09 (sick day 07-08 attributed THERE), HISJ from 07-10
    (he worked 07-13 here, and HISJ owns the paycheck). HISJ's run must
    pay the worked day and NONE of SSSJ's sick — paying it here would pay
    it twice once SSSJ runs."""
    from tests.employees import make_employee, set_rate

    dept_id, pos_id, device_id = _seed(db_session)
    sssj_dept = _second_property(db_session)
    # Wanda keeps the HISJ run alive independently of Hank's shape.
    wanda = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, wanda, 1)

    mover = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                          position_id=pos_id, full_name="Mo V",
                          pay_type="hourly")
    db_session.flush()
    hisj = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == mover.employee_id)
    ).scalar_one()
    hisj.effective_from = date(2026, 7, 10)
    db_session.add(EmployeeAssignment(
        employee_id=mover.employee_id, property_id="SSSJ",
        department_id=sssj_dept, is_primary=True, status="active",
        effective_from=date(2026, 1, 5), effective_to=date(2026, 7, 10),
    ))
    db_session.flush()
    set_rate(db_session, hisj, "20.00", effective_from=date(2026, 7, 10))
    sssj_assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == mover.employee_id,
            EmployeeAssignment.property_id == "SSSJ")
    ).scalar_one()
    set_rate(db_session, sssj_assignment, "20.00")
    from tests.test_e5_provider_port import _ssn_profile

    _ssn_profile(db_session, mover.employee_id)
    _chain_row(db_session, mover.employee_id, 1)
    _shift(db_session, device_id, mover.employee_id, 13, 8, 16)
    db_session.commit()
    _approved_card(db_session, mover.employee_id)
    _sick(db_session, mover.employee_id, "8.00", date(2026, 7, 8))  # SSSJ's

    report = _assemble(db_session)
    assert report.ok, report.problems
    entry = next(e for e in report.entries
                 if e.employee_id == mover.employee_id)
    assert entry.regular_hours == Decimal("8.00")
    assert entry.sick_hours == Decimal("0.00"), "SSSJ's day paid from HISJ"


def test_two_sick_days_at_two_rates_refuse_by_name(db_session):
    """S4: the only mixed-rate test crossed the worked/sick line; two SICK
    days at two rates never occurred. A raise dated between two sick days
    must refuse — the silent alternative priced sick at the worked rate."""
    from tests.employees import set_rate
    from usali.models import AssignmentRate

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == hank)
    ).scalar_one()
    open_rate = db_session.execute(
        select(AssignmentRate).where(
            AssignmentRate.assignment_id == assignment.assignment_id,
            AssignmentRate.rate_type == "regular",
        )
    ).scalar_one()
    open_rate.effective_to = date(2026, 7, 10)
    set_rate(db_session, assignment, "25.00", effective_from=date(2026, 7, 10))
    db_session.commit()
    _sick(db_session, hank, "8.00", date(2026, 7, 8))   # at $20
    _sick(db_session, hank, "8.00", date(2026, 7, 15))  # at $25

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named
    assert any("across the sick days taken" in p for p in named)


def test_sick_of_someone_this_run_does_not_pay_blocks_by_name(db_session):
    """S5: Hank's primary was HISJ through 07-09 (his sick day 07-08 is
    attributed HERE) but SSSJ from 07-10 — SSSJ owns the paycheck. HISJ's
    run cannot pay him, and silence would pay the day from NOWHERE: the
    'this run does not pay this employee' blocker must name him."""
    from tests.employees import make_employee, set_rate

    dept_id, pos_id, device_id = _seed(db_session)
    sssj_dept = _second_property(db_session)
    wanda = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, wanda, 1)

    mover = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                          position_id=pos_id, full_name="Mo V",
                          pay_type="hourly")
    db_session.flush()
    hisj = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == mover.employee_id)
    ).scalar_one()
    hisj.effective_to = date(2026, 7, 10)
    set_rate(db_session, hisj, "20.00")
    db_session.add(EmployeeAssignment(
        employee_id=mover.employee_id, property_id="SSSJ",
        department_id=sssj_dept, is_primary=True, status="active",
        effective_from=date(2026, 7, 10),
    ))
    from tests.test_e5_provider_port import _ssn_profile

    _ssn_profile(db_session, mover.employee_id)
    _chain_row(db_session, mover.employee_id, 1)
    db_session.commit()
    _sick(db_session, mover.employee_id, "8.00", date(2026, 7, 8))  # HISJ's

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Mo V" in p]
    assert named and "does not pay this employee" in named[0]


def test_a_sick_only_person_missing_their_chain_blocks_not_crashes(db_session):
    """S6: the sick-only loop's person-guards were unexercised — disabling
    them let preflight go green and the run 500 mid-flight on the chain
    assert, after the PayRun row existed. The deposit blocker must name
    them at preflight."""
    from tests.employees import make_employee

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    solo = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Sol O",
                         pay_type="hourly", pay_rate="22.00")
    db_session.flush()
    from tests.test_e5_provider_port import _ssn_profile

    _ssn_profile(db_session, solo.employee_id)  # profile but NO chain
    db_session.commit()
    _sick(db_session, solo.employee_id, "8.00", _SICK_DAY)

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Sol O" in p]
    assert named and "deposit" in named[0]


def test_a_refused_card_holder_is_not_re_paid_by_the_sick_loop(db_session):
    """S7: the documented 'card holders are NOT re-processed' invariant was
    unpinned. An unapproved card refuses in the card loop; the sick-only
    loop must not then submit the same person's sick hours around the
    refusal."""
    from tests.employees import make_employee

    from usali.timecards import assemble_timecard

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    pend = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Pen D",
                         pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    from tests.test_e5_provider_port import _ssn_profile

    _ssn_profile(db_session, pend.employee_id)
    _chain_row(db_session, pend.employee_id, 1)
    _shift(db_session, device_id, pend.employee_id, 7, 8, 16)
    db_session.commit()
    assemble_timecard(db_session, pend.employee_id, _PERIOD_DAY,
                      anchor=_ANCHOR)  # NOT approved
    db_session.commit()
    _sick(db_session, pend.employee_id, "8.00", _SICK_DAY)

    report = _assemble(db_session)
    assert not report.ok
    named = [p for p in report.problems if "Pen D" in p]
    assert len(named) == 1 and "not approved" in named[0]
    assert not any(e.employee_id == pend.employee_id for e in report.entries)


def test_a_late_void_of_paid_sick_logs_and_does_not_block(db_session, caplog):
    """S8: the paid-then-voided branch (money moved, books now say never
    taken) was entirely unpinned — it must LOG server-side, never become
    a permanent preflight line (the E3 lesson)."""
    import logging

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    wanda = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, wanda, 1)
    _sick(db_session, hank, "8.00", _SICK_DAY)
    provider = InMemoryPayrollProvider()
    _execute(db_session, provider)  # run PAYS the sick day
    time.sleep(0.05)
    _sick(db_session, hank, "8.00", _SICK_DAY, entry_type="usage_void")

    _shift(db_session, device_id, wanda, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, wanda, period_day=_NEXT_PERIOD_DAY)

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        report = assemble_pay_run_entries(
            db_session, "HISJ", _NEXT_PERIOD_DAY, anchor=_ANCHOR,
            provider_capabilities=provider.capabilities(),
        )
    assert not any("Hank H" in p for p in report.problems)
    assert any("voided after" in r.getMessage() for r in caplog.records)


def test_same_content_is_never_stale_whatever_the_clocks_say(db_session):
    """S9, SUPERSEDED by H7 (decision 8): the original pinned the
    equal-timestamp boundary of the retired created_at-vs-synced_at
    predicate. The fingerprint predicate has no boundary to pin — so this
    now asserts the STRONGER claim that subsumes it: identical content is
    never stale no matter what the clocks say. The chain rows' created_at
    is shoved a day PAST synced_at (the transaction-start race shape that
    used to read stale and re-send PII); content unchanged → not stale."""
    from datetime import timedelta

    from sqlalchemy import update as sql_update

    from tests.test_provider_resync import _synced_world
    from usali.models import DepositAccount
    from usali.payroll_run import provider_payload_stale

    emp_id, provider, ref = _synced_world(db_session)
    db_session.execute(
        sql_update(DepositAccount)
        .where(DepositAccount.employee_id == emp_id)
        .values(created_at=ref.synced_at + timedelta(days=1))
    )
    db_session.commit()
    assert provider_payload_stale(db_session, emp_id, ref) is False


def test_an_over_void_clamps_at_zero_not_negative(db_session):
    """S10: every void test used void == usage exactly, leaving the
    documented clamp unpinned — an over-void yielded NEGATIVE hours into
    the submission and the SOS. 8h usage + 12h void = zero taken, never
    minus four."""
    from usali.sick_leave import sick_days_taken

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "8.00", _SICK_DAY)
    _sick(db_session, hank, "12.00", _SICK_DAY, entry_type="usage_void")

    assert sick_days_taken(db_session, _PERIOD_DAY, _NEXT_PERIOD_DAY) == []
    report = _assemble(db_session)
    assert report.ok, report.problems
    entry = next(e for e in report.entries if e.employee_id == hank)
    assert entry.sick_hours == Decimal("0")


# --- PII lens P1: tax elections stay sealed ----------------------------------


def test_sync_opens_no_tax_elections(db_session):
    """P1 (PII lens, Medium): neither adapter serializes tax elections, so
    opening the sealed value put plaintext in transit with no carrier —
    and G2's re-sync made it recurring. A profile WITH a sealed election
    must sync with exactly ssn + chain opens (3), nothing more."""
    from sqlalchemy import update as sql_update

    from tests.test_provider_resync import _CountingOpener
    from usali.models import EmployeePayrollProfile
    from usali.payroll_run import sync_employees
    from tests.test_e5_provider_port import _sealed_to

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    db_session.execute(
        sql_update(EmployeePayrollProfile)
        .where(EmployeePayrollProfile.employee_id == hank)
        .values(tax_elections_sealed=_sealed_to(
            _OPENER, hank, "tax_elections", b"single-0"))
    )
    db_session.commit()

    counting = _CountingOpener(_OPENER)
    provider = InMemoryPayrollProvider()
    sync_employees(db_session, provider, counting,
                   provider_name="memory", employee_ids=[hank])
    assert counting.opens == 3  # ssn + routing + account; NOT tax elections
    [stored] = provider.stored_employees().values()
    assert stored.tax_elections is None


# --- PII lens P4: mock shape-mismatch 422s must not echo the body ------------


def test_mock_shape_mismatch_422_never_echoes_the_body():
    """P4: FastAPI's default RequestValidationError response includes the
    offending `input` — the full request body, SSN and bank included. The
    mocks' posture is 'bodies are never echoed', full stop."""
    from fastapi.testclient import TestClient

    from usali.adp_mock import create_mock_adp
    from usali.gusto_mock import create_mock_gusto

    for app, path in (
        (create_mock_gusto(), "/v1/companies/c1/employees"),
        (create_mock_adp(), "/hr/v1/workers"),
    ):
        resp = TestClient(app).post(path, json=["123-45-6789", "000123456"])
        assert resp.status_code == 422
        assert "123-45-6789" not in resp.text
        assert "000123456" not in resp.text
        assert resp.json() == {"detail": "invalid request shape"}


# --- Tests-lens coverage gap: fetch with a sick-only employee ----------------


def test_fetch_attributes_a_sick_only_employees_money(db_session):
    """A sick-only entrant has no timecard, so the apportionment falls back
    to the paying property — their money and census row must land, not
    vanish from the books (they are IN the suppression census)."""
    from tests.employees import make_employee

    from usali.models import PayRunLineProperty
    from usali.payroll_run import fetch_pay_run_results

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    solo = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Sol O",
                         pay_type="hourly", pay_rate="22.00")
    db_session.flush()
    from tests.test_e5_provider_port import _ssn_profile

    _ssn_profile(db_session, solo.employee_id)
    _chain_row(db_session, solo.employee_id, 1)
    db_session.commit()
    _sick(db_session, solo.employee_id, "8.00", _SICK_DAY)

    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted"
    written = fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    assert written == 2
    census = db_session.execute(
        select(PayRunLineProperty).where(
            PayRunLineProperty.pay_run_id == run.pay_run_id,
            PayRunLineProperty.employee_id == solo.employee_id,
        )
    ).scalar_one()
    assert census.property_id == "HISJ"
    assert Decimal(str(census.hours)) == Decimal("8.00")
    assert Decimal(str(census.gross)) == Decimal("176.00")  # 8h x $22
    # H5 (decision 5): the line stores what it paid as sick — for a
    # sick-only entrant, everything.
    from usali.models import PayRunLine

    line = db_session.execute(
        select(PayRunLine).where(
            PayRunLine.pay_run_id == run.pay_run_id,
            PayRunLine.employee_id == solo.employee_id,
        )
    ).scalar_one()
    assert Decimal(str(line.sick_hours)) == Decimal("8.00")
