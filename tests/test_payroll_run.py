"""Pay-run assembly + preflight + provider round-trip (Pillar C2, Tasks 6-7).

Seeding mirrors tests/test_labor_promote.py, extended with sealed payroll
profiles (via seal_for_test + a module-level SoftwareOpener) and a PaySchedule.
Preflight checks PRESENCE of sealed PII only — it must never open an envelope,
and problem strings must never contain a PII value.

Task 7 (sync/submit/fetch) is the PII-transit moment: the vault opens ONLY
inside sync_employees, and the InMemory provider's _employees list proves what
plaintext the provider actually received. A failed run's failure_reason must
never carry a PII value — pinned below.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.employees import make_employee
from tests.grants import grant_role
from usali.models import (
    Department,
    EmployeePayrollProfile,
    KioskDevice,
    Organization,
    PayRun,
    PayRunLine,
    PaySchedule,
    Position,
    Property,
    ProviderEmployeeRef,
    Punch,
    Timecard,
    UsaliActualLaborFact,
)
from usali.opener import SoftwareOpener, seal_for_test
from usali.payroll_provider import (
    InMemoryPayrollProvider,
    PayRunResult,
    PayRunResultLine,
    ProviderError,
)
from usali.payroll_run import (
    PayRunBlocked,
    PayRunConflict,
    assemble_pay_run_entries,
    execute_pay_run,
    fetch_pay_run_results,
)
from usali.timecards import assemble_timecard
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)  # Monday; period_for(2026-07-06) == 07-06..07-19
_PERIOD_DAY = date(2026, 7, 6)

# Module-level opener so every sealed profile in this file shares one keypair
# (Task 7 opens them with the same instance).
_OPENER = SoftwareOpener.generate(key_id="test-payrun-1")

_SSN = b"123-45-6789"
_BANK_ACCOUNT = b"000123456"
_BANK_ROUTING = b"021000021"


def _seed(db_session, *, schedule=True):
    """Org + property HISJ + Housekeeping dept + non-exempt position + kiosk
    (+ biweekly PaySchedule unless schedule=False). Returns ids."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Room Attendant", flsa_exempt=False)
    db_session.add(pos)
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64, enrolled_by="adm")
    db_session.add(device)
    if schedule:
        db_session.add(PaySchedule(
            property_id="HISJ", frequency="biweekly", anchor=_ANCHOR,
            check_date_offset_days=5,
        ))
    db_session.flush()
    # L4: role authority is DB grants, not token roles — plant the
    # personas the payroll API tests mint against this world.
    grant_role(db_session, "payroll_admin", sub="pa")
    grant_role(db_session, "accountant", sub="acct")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    grant_role(db_session, "org_admin", sub="adm")
    return dept.department_id, pos.position_id, device.device_id


def _employee(db_session, dept_id, pos_id, *, name="Hank H", pay_rate="20.00",
              property_id="HISJ", profile=True):
    emp = make_employee(db_session, property_id=property_id, department_id=dept_id, position_id=pos_id,
                   full_name=name, pay_type="hourly", pay_rate=pay_rate)
    db_session.add(emp)
    db_session.flush()
    if profile:
        _sealed_profile(db_session, emp.employee_id)
    return emp.employee_id


def _sealed_profile(db_session, emp_id):
    """Identity PII on the profile + a one-remainder deposit chain — the shape
    production holds after E5. The deposit envelopes seal under the NEW
    per-ordinal slot aads; legacy-slot rows are exercised in
    test_e5_provider_port.py, not here."""
    from usali.deposit_accounts import account_slot, routing_slot
    from usali.models import DepositAccount

    def sealed(slot, plaintext):
        return seal_for_test(
            _OPENER.public_key(), plaintext, aad=f"{emp_id}:{slot}".encode()
        ).to_json()

    db_session.add(EmployeePayrollProfile(
        employee_id=emp_id,
        ssn_sealed=sealed("ssn", _SSN),
    ))
    db_session.add(DepositAccount(
        employee_id=emp_id, ordinal=1, allocation_type="remainder",
        allocation_value=None, account_type="checking",
        sealed_account=sealed(account_slot(1, False), _BANK_ACCOUNT),
        sealed_routing=sealed(routing_slot(1, False), _BANK_ROUTING),
        legacy_sealed=False,
    ))
    db_session.flush()


def _shift(db_session, device_id, emp_id, day, in_h, out_h):
    for ptype, h in (("clock_in", in_h), ("clock_out", out_h)):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(2026, 7, day, h, tzinfo=UTC),
            business_date=date(2026, 7, day),
            photo_key=f"k/{emp_id}-{ptype}{day}{h}",
        ))


def _approved_card(db_session, emp_id, period_day=_PERIOD_DAY):
    card = assemble_timecard(db_session, emp_id, period_day, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    return card


def test_assemble_builds_one_entry_per_employee_with_ot_split(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _employee(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 6, 8, 20)  # 12h Monday → 8 reg + 4 OT
    db_session.commit()
    _approved_card(db_session, emp_id)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert report.ok
    assert report.problems == []
    [e] = report.entries
    assert e.employee_id == emp_id
    assert (e.regular_hours, e.ot_hours, e.dt_hours) == (
        Decimal("8.00"), Decimal("4.00"), Decimal("0.00")
    )
    assert e.hourly_rate == Decimal("20.00")
    assert (report.period_start, report.period_end) == (date(2026, 7, 6), date(2026, 7, 19))
    assert report.check_date == date(2026, 7, 24)  # period_end 07-19 + 5


def test_preflight_names_employees_missing_rate_or_pii(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    no_rate_id = _employee(db_session, dept_id, pos_id, name="Norah NoRate",
                           pay_rate=None, profile=True)
    no_pii_id = _employee(db_session, dept_id, pos_id, name="Pete NoPII",
                          pay_rate="18.00", profile=False)
    for emp_id in (no_rate_id, no_pii_id):
        _shift(db_session, device_id, emp_id, 6, 9, 17)
        db_session.commit()
        _approved_card(db_session, emp_id)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    assert report.entries == []
    text = " ".join(report.problems)
    # BOTH employees are named, with what is missing.
    assert f"employee {no_rate_id} (Norah NoRate)" in text and "pay rate" in text
    assert f"employee {no_pii_id} (Pete NoPII)" in text
    # The deposit destination left the profile in E5: a missing-ssn profile
    # blocks before the chain check, and chain blockers have their own tests
    # (test_e5_provider_port).
    assert "ssn" in text
    # Never a PII VALUE — only field names.
    for pii in ("123-45-6789", "000123456", "021000021", "18.00", "20.00"):
        assert pii not in text


def test_preflight_requires_a_pay_schedule(db_session):
    dept_id, pos_id, device_id = _seed(db_session, schedule=False)
    emp_id = _employee(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 6, 9, 17)
    db_session.commit()
    _approved_card(db_session, emp_id)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    assert report.check_date is None
    assert any("pay schedule" in p for p in report.problems)


def test_only_approved_timecards_are_included(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    approved_id = _employee(db_session, dept_id, pos_id, name="Alice Approved")
    open_id = _employee(db_session, dept_id, pos_id, name="Oscar Open")
    _shift(db_session, device_id, approved_id, 6, 9, 17)
    _shift(db_session, device_id, open_id, 6, 9, 17)
    db_session.commit()
    _approved_card(db_session, approved_id)
    open_card = assemble_timecard(db_session, open_id, _PERIOD_DAY, anchor=_ANCHOR)
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    # The open card contributes nothing AND is a named blocker.
    assert not report.ok
    [e] = report.entries
    assert e.employee_id == approved_id
    text = " ".join(report.problems)
    assert f"employee {open_id} (Oscar Open)" in text
    assert f"timecard {open_card.timecard_id} is not approved" in text


def test_exempt_employee_gets_flat_hours_no_ot(db_session):
    dept_id, _pos_id, device_id = _seed(db_session)
    exempt_pos = Position(department_id=dept_id, title="Manager", flsa_exempt=True)
    db_session.add(exempt_pos)
    db_session.flush()
    emp_id = _employee(db_session, dept_id, exempt_pos.position_id,
                       name="Eve Exempt", pay_rate="30.00")
    _shift(db_session, device_id, emp_id, 6, 8, 20)  # 12h, but exempt → no OT
    db_session.commit()
    _approved_card(db_session, emp_id)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert report.ok
    [e] = report.entries
    assert (e.regular_hours, e.ot_hours, e.dt_hours) == (
        Decimal("12.00"), Decimal("0.00"), Decimal("0.00")
    )
    assert e.hourly_rate == Decimal("30.00")


def test_no_timecards_is_a_named_problem(db_session):
    dept_id, pos_id, _device_id = _seed(db_session)
    _employee(db_session, dept_id, pos_id)
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    assert any("no timecards" in p for p in report.problems)


def test_other_property_timecards_never_leak_in(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _employee(db_session, dept_id, pos_id)
    _shift(db_session, device_id, emp_id, 6, 9, 17)
    db_session.commit()
    _approved_card(db_session, emp_id)

    # A second property with its own employee, SAME period_start. Give the
    # foreign employee no pay_rate and no profile — if it leaked into HISJ's
    # assembly it would surface as a problem, not just an extra entry.
    db_session.add(Property(property_id="OTHR", org_id=1, name="Other", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    other_dept = Department(property_id="OTHR", name="Front Desk")
    db_session.add(other_dept)
    db_session.flush()
    other_device = KioskDevice(property_id="OTHR", name="iPad2", token_hash="x" * 64,
                               enrolled_by="adm")
    db_session.add(other_device)
    db_session.flush()
    other_emp = _employee(db_session, other_dept.department_id, None,
                          name="Frank Foreign", pay_rate=None,
                          property_id="OTHR", profile=False)
    _shift(db_session, other_device.device_id, other_emp, 6, 9, 17)
    db_session.commit()
    _approved_card(db_session, other_emp)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert report.ok
    assert report.problems == []
    [e] = report.entries
    assert e.employee_id == emp_id
    assert "Frank Foreign" not in " ".join(report.problems)


# --- Task 7: sync + submit + fetch — the vault opens at send -----------------


class _FailingSubmitProvider(InMemoryPayrollProvider):
    """Sync succeeds (refs get created); submit blows up like a provider 500."""

    def submit_pay_run(self, *, period_start, period_end, check_date, entries):
        raise ProviderError("mem payroll submit failed (500): internal error")


class _FailingSyncProvider(InMemoryPayrollProvider):
    """Sync blows up like a provider 422 — BEFORE any submit."""

    def sync_employee(self, employee):
        raise ProviderError("mem employee sync failed (422)")


class _StillProcessingProvider(InMemoryPayrollProvider):
    """The async-reality case: the run exists but is not processed yet."""

    def get_pay_run(self, provider_run_id):
        return PayRunResult(status="submitted", lines=[])


class _UnknownRefProvider(InMemoryPayrollProvider):
    """Returns a payment for an employee ref we never synced."""

    def get_pay_run(self, provider_run_id):
        return PayRunResult(status="processed", lines=[
            PayRunResultLine(provider_employee_id="ghost", gross=Decimal("1.00"),
                             employee_taxes=Decimal("0.15"),
                             employer_taxes=Decimal("0.10"), net=Decimal("0.85")),
        ])


def _two_employees_16h_each(db_session):
    """2 employees, same dept, $20/h, two 8h days (16h regular, no OT) in the
    2026-07-06..07-19 period, approved cards + sealed profiles + schedule."""
    dept_id, pos_id, device_id = _seed(db_session)
    ids = []
    for name in ("Hank H", "Ivy I"):
        emp_id = _employee(db_session, dept_id, pos_id, name=name)
        _shift(db_session, device_id, emp_id, 6, 9, 17)
        _shift(db_session, device_id, emp_id, 7, 9, 17)
        db_session.commit()
        _approved_card(db_session, emp_id)
        ids.append(emp_id)
    return device_id, ids


def _execute(db_session, provider, *, in_period=_PERIOD_DAY):
    return execute_pay_run(db_session, "HISJ", in_period, anchor=_ANCHOR,
                           provider=provider, provider_name="mem",
                           opener=_OPENER, actor="pa")


def test_execute_pay_run_round_trips_and_aggregates(db_session):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    db_session.commit()
    assert run.status == "submitted" and run.provider_run_id
    assert run.submitted_at is not None

    n = fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    assert n == 2 and run.status == "processed"
    assert run.processed_at is not None

    lines = db_session.execute(select(PayRunLine)).scalars().all()
    assert len(lines) == 2
    assert Decimal(lines[0].gross) == Decimal("320.00")   # decrypted transparently
    assert Decimal(lines[0].net) == Decimal("272.00")     # 320 - 15%
    assert Decimal(str(lines[0].hours)) == Decimal("16.00")

    facts = db_session.execute(select(UsaliActualLaborFact)).scalars().all()
    [fact] = facts                                          # ONE dept aggregate
    assert Decimal(str(fact.gross)) == Decimal("640.00")
    assert Decimal(str(fact.employer_burden)) == Decimal("64.00")  # 10% of 640
    assert Decimal(str(fact.hours)) == Decimal("32.00")
    assert fact.pay_run_id == run.pay_run_id
    assert (fact.period_start, fact.period_end) == (date(2026, 7, 6), date(2026, 7, 19))

    # The provider received PLAINTEXT PII (decrypt-and-send) — and the fake
    # proves the vault round-trip: the values the provider saw are the seeded ones.
    assert provider._employees[0].ssn == "123-45-6789"
    [acct] = provider._employees[0].deposit_accounts
    assert (acct.account, acct.routing) == ("000123456", "021000021")
    assert acct.allocation_type == "remainder"
    # But nothing PERSISTED carries plaintext: the audit trail + run rows don't.
    assert run.failure_reason is None


def test_sync_is_idempotent_per_provider(db_session):
    device_id, ids = _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    _execute(db_session, provider)
    db_session.commit()
    assert len(provider._employees) == 2

    # A SECOND period (07-20..08-02) with fresh approved cards: no re-sync.
    for emp_id in ids:
        _shift(db_session, device_id, emp_id, 20, 9, 17)
        db_session.commit()
        _approved_card(db_session, emp_id, period_day=date(2026, 7, 20))
    run2 = _execute(db_session, provider, in_period=date(2026, 7, 20))
    db_session.commit()
    assert run2.status == "submitted"
    assert len(provider._employees) == 2  # not 4
    refs = db_session.execute(select(ProviderEmployeeRef)).scalars().all()
    assert len(refs) == 2


def test_refetch_replaces_not_duplicates(db_session):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    db_session.commit()
    for _ in range(2):
        fetch_pay_run_results(db_session, run, provider=provider)
        db_session.commit()

    assert len(db_session.execute(select(PayRunLine)).scalars().all()) == 2
    [fact] = db_session.execute(select(UsaliActualLaborFact)).scalars().all()
    assert Decimal(str(fact.gross)) == Decimal("640.00")  # replaced, not doubled
    assert run.status == "processed"


def test_preflight_failure_refuses_to_execute(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _employee(db_session, dept_id, pos_id, name="Norah NoRate",
                       pay_rate=None)
    _shift(db_session, device_id, emp_id, 6, 9, 17)
    db_session.commit()
    _approved_card(db_session, emp_id)
    provider = InMemoryPayrollProvider()

    with pytest.raises(PayRunBlocked) as exc:
        _execute(db_session, provider)
    assert "no pay rate on file" in str(exc.value)
    assert "Norah NoRate" in str(exc.value)
    # NOTHING created, NO provider call.
    assert db_session.execute(select(PayRun)).scalars().all() == []
    assert db_session.execute(select(ProviderEmployeeRef)).scalars().all() == []
    assert provider._employees == []


def test_provider_failure_marks_run_failed_and_replacement_is_allowed(db_session):
    _two_employees_16h_each(db_session)
    failing = _FailingSubmitProvider()
    run = _execute(db_session, failing)
    db_session.commit()
    assert run.status == "failed"
    assert run.failure_reason and "500" in run.failure_reason
    # The failure reason must NEVER carry PII (the sync DID see plaintext).
    for pii in ("123-45-6789", "000123456", "021000021"):
        assert pii not in run.failure_reason
    assert len(failing._employees) == 2  # sync succeeded before the submit blew up

    # A failed run has NO lines (lines are inserted only on submit SUCCESS).
    assert db_session.execute(select(PayRunLine)).scalars().all() == []

    # Re-POSTing the SAME period REPLACES the failed run...
    good = InMemoryPayrollProvider()
    run2 = _execute(db_session, good)
    db_session.commit()
    assert run2.status == "submitted" and run2.pay_run_id != run.pay_run_id
    [only] = db_session.execute(select(PayRun)).scalars().all()
    assert only.pay_run_id == run2.pay_run_id
    # ...without orphaning lines (every line belongs to the replacement's
    # successful submit) and WITHOUT losing the employee refs: the earlier
    # sync succeeded, so the replacement run re-syncs nobody.
    lines = db_session.execute(select(PayRunLine)).scalars().all()
    assert {line.pay_run_id for line in lines} == {run2.pay_run_id}
    assert len(lines) == 2
    assert len(db_session.execute(select(ProviderEmployeeRef)).scalars().all()) == 2
    assert good._employees == []


def test_duplicate_period_raises_pay_run_conflict(db_session):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    _execute(db_session, provider)
    db_session.commit()

    with pytest.raises(PayRunConflict) as exc:
        _execute(db_session, provider)
    assert "already exists" in str(exc.value)
    assert issubclass(PayRunConflict, PayRunBlocked)  # 409 is a kind of blocked
    assert len(db_session.execute(select(PayRun)).scalars().all()) == 1


def test_fetch_before_submit_raises(db_session):
    _two_employees_16h_each(db_session)
    failing = _FailingSubmitProvider()
    run = _execute(db_session, failing)  # failed → provider_run_id is None
    db_session.commit()
    with pytest.raises(ValueError, match="never submitted"):
        fetch_pay_run_results(db_session, run, provider=failing)


def test_fetch_while_still_processing_writes_nothing(db_session):
    _two_employees_16h_each(db_session)
    provider = _StillProcessingProvider()
    run = _execute(db_session, provider)
    db_session.commit()
    assert fetch_pay_run_results(db_session, run, provider=provider) == 0
    assert run.status == "submitted"  # polling semantics: try again later
    # The submit-time hours snapshot is untouched: money still the placeholder.
    lines = db_session.execute(select(PayRunLine)).scalars().all()
    assert len(lines) == 2
    assert all(Decimal(line.gross) == 0 for line in lines)
    assert db_session.execute(select(UsaliActualLaborFact)).scalars().all() == []


def test_fetch_with_unknown_provider_ref_raises(db_session):
    _two_employees_16h_each(db_session)
    provider = _UnknownRefProvider()
    run = _execute(db_session, provider)
    db_session.commit()
    with pytest.raises(ValueError, match="unknown employee ref"):
        fetch_pay_run_results(db_session, run, provider=provider)


def test_sync_failure_marks_run_failed_and_is_replaceable(db_session):
    """A ProviderError from sync (BEFORE submit) must be handled exactly like a
    submit failure: run persisted as failed with a failure_reason, no uncaught
    exception, no lines — and the failed run is replaceable by re-POST."""
    _two_employees_16h_each(db_session)
    failing = _FailingSyncProvider()
    run = _execute(db_session, failing)
    db_session.commit()
    assert run.status == "failed"
    assert run.failure_reason and "422" in run.failure_reason
    # Sync failed before any submit: no lines, no refs, no plaintext anywhere.
    assert db_session.execute(select(PayRunLine)).scalars().all() == []
    for pii in ("123-45-6789", "000123456", "021000021"):
        assert pii not in run.failure_reason

    # The failed run is replaceable like any other failure.
    good = InMemoryPayrollProvider()
    run2 = _execute(db_session, good)
    db_session.commit()
    assert run2.status == "submitted" and run2.pay_run_id != run.pay_run_id
    [only] = db_session.execute(select(PayRun)).scalars().all()
    assert only.pay_run_id == run2.pay_run_id


def test_lines_exist_with_hours_immediately_after_submit(db_session):
    """FIX 2: submit success snapshots per-employee hours into PayRunLine rows
    immediately (money is the encrypted placeholder "0" until fetch)."""
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    db_session.commit()
    assert run.status == "submitted"

    lines = db_session.execute(select(PayRunLine)).scalars().all()
    assert len(lines) == 2
    for line in lines:
        assert line.pay_run_id == run.pay_run_id
        assert Decimal(str(line.hours)) == Decimal("16.00")
        assert Decimal(line.gross) == 0
        assert Decimal(line.employee_taxes) == 0
        assert Decimal(line.employer_taxes) == 0
        assert Decimal(line.net) == 0


def test_fetch_uses_submitted_hours_not_live_timecards(db_session):
    """THE DESYNC REGRESSION: a timecard change between submit and fetch must
    NOT change the stored hours — the provider priced the SUBMITTED hours.
    Worst case pinned here: a card un-approved after submit used to recompute
    to hours=0 next to nonzero gross."""
    device_id, ids = _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    db_session.commit()

    # Un-approve one employee's card directly in the DB after submit.
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == ids[0])
    ).scalar_one()
    card.status = "open"
    db_session.commit()

    n = fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()
    assert n == 2 and run.status == "processed"

    # Stored hours UNCHANGED (the submitted snapshot), money from the provider.
    lines = db_session.execute(select(PayRunLine)).scalars().all()
    assert len(lines) == 2
    for line in lines:
        assert Decimal(str(line.hours)) == Decimal("16.00")
        assert Decimal(line.gross) == Decimal("320.00")

    # Facts aggregate the STORED hours + provider money.
    [fact] = db_session.execute(select(UsaliActualLaborFact)).scalars().all()
    assert Decimal(str(fact.hours)) == Decimal("32.00")
    assert Decimal(str(fact.gross)) == Decimal("640.00")
