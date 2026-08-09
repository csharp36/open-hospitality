"""E5 Task 4: the provider port carries a CHAIN, and preflight guards it.

This file exercises the PII-transit moment: `sync_employees` is the only
place plaintext account numbers exist, opened per-row with the aad derived
from row identity — `{employee_id}:deposit:{ordinal}:{field}` for chain rows,
the legacy `{employee_id}:bank_account` / `bank_routing` slots for backfilled
rows (`legacy_sealed`). A spliced envelope — right key, wrong slot — must
FAIL to open, which refuses the run rather than paying into the wrong
account.
"""

from datetime import date
from decimal import Decimal

import pytest

from tests.employees import make_employee
from tests.test_payroll_run import _seed, _shift, _approved_card
from usali.deposit_accounts import account_slot, routing_slot
from usali.models import DepositAccount, EmployeePayrollProfile
from usali.opener import SoftwareOpener, seal_for_test
from usali.payroll_provider import InMemoryPayrollProvider, PlainDepositAccount
from usali.payroll_run import assemble_pay_run_entries, sync_employees

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)
_OPENER = SoftwareOpener.generate(key_id="port-test-1")


def _sealed_to(opener, emp_id, slot, plaintext):
    return seal_for_test(
        opener.public_key(), plaintext, aad=f"{emp_id}:{slot}".encode()
    ).to_json()


def _ssn_profile(db_session, emp_id):
    db_session.add(EmployeePayrollProfile(
        employee_id=emp_id,
        ssn_sealed=_sealed_to(_OPENER, emp_id, "ssn", b"123-45-6789"),
    ))
    db_session.flush()


def _chain_row(db_session, emp_id, ordinal, *, allocation_type="remainder",
               allocation_value=None, legacy=False, account=b"000123456",
               routing=b"021000021", sealed_account=None, sealed_routing=None):
    db_session.add(DepositAccount(
        employee_id=emp_id, ordinal=ordinal, allocation_type=allocation_type,
        allocation_value=allocation_value, account_type="checking",
        sealed_account=(
            sealed_account if sealed_account is not None
            else _sealed_to(_OPENER, emp_id, account_slot(ordinal, legacy), account)
        ),
        sealed_routing=(
            sealed_routing if sealed_routing is not None
            else _sealed_to(_OPENER, emp_id, routing_slot(ordinal, legacy), routing)
        ),
        legacy_sealed=legacy,
    ))
    db_session.flush()


def _payable_employee(db_session, dept_id, pos_id, device_id, *, name="Hank H"):
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name=name,
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _ssn_profile(db_session, emp.employee_id)
    _shift(db_session, device_id, emp.employee_id, 6, 8, 16)
    db_session.commit()
    _approved_card(db_session, emp.employee_id)
    return emp.employee_id


# --- preflight: the chain is guarded by NAME ---------------------------------


def test_preflight_names_an_employee_with_no_deposit_accounts(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    _payable_employee(db_session, dept_id, pos_id, device_id)
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "deposit" in named[0]


def test_preflight_names_a_half_sealed_backfilled_row(db_session):
    """The '' placeholder from the e5a0 backfill reads as not-on-file — the
    same named blocker the missing COLUMN produced before E5."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1, legacy=True, sealed_routing="")
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "routing" in named[0]


def test_preflight_names_an_illegal_chain_that_got_into_the_db(db_session):
    """The schema cannot express every illegality (a no-remainder chain is
    insertable row by row). allocation_violation runs again here — defence in
    depth, same function as the API door, so the two cannot drift."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1, allocation_type="percent",
               allocation_value=Decimal("50.00"))
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "remainder" in named[0]


def test_preflight_passes_a_legal_complete_chain(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1, allocation_type="amount",
               allocation_value=Decimal("50.00"))
    _chain_row(db_session, emp_id, 2)
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert report.ok, report.problems


# --- sync: plaintext exists per-row, transiently, slot-bound -----------------


def test_sync_opens_a_new_chain_with_per_ordinal_aads(db_session):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1, allocation_type="amount",
               allocation_value=Decimal("50.00"),
               account=b"111000111", routing=b"021000021")
    _chain_row(db_session, emp_id, 2, account=b"222000222",
               routing=b"121000248")
    db_session.commit()

    provider = InMemoryPayrollProvider()
    refs = sync_employees(db_session, provider, _OPENER,
                          provider_name="memory", employee_ids=[emp_id])
    assert refs[emp_id] == "mem-1"
    [sent] = provider._employees
    assert [
        (a.ordinal, a.allocation_type, a.account, a.routing)
        for a in sent.deposit_accounts
    ] == [
        (1, "amount", "111000111", "021000021"),
        (2, "remainder", "222000222", "121000248"),
    ]


def test_sync_opens_a_backfilled_row_with_the_legacy_aad(db_session):
    """THE migration-consequence test: e5a0 copied ciphertext sealed under
    `{employee_id}:bank_account` / `bank_routing`. The open path must derive
    those slots for legacy rows or every backfilled employee's first pay run
    after E5 fails."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1, legacy=True,
               account=b"333000333", routing=b"021000021")
    db_session.commit()

    provider = InMemoryPayrollProvider()
    sync_employees(db_session, provider, _OPENER,
                   provider_name="memory", employee_ids=[emp_id])
    [sent] = provider._employees
    [acct] = sent.deposit_accounts
    assert acct.account == "333000333"


def test_a_spliced_envelope_refuses_the_run_by_name(db_session):
    """Right key, wrong slot: ordinal 2's envelope moved into ordinal 1's row
    (the swap that would redirect a paycheck), driven through the REAL run
    entry point. The E5 review found this test's first draft vacuous: it
    called sync_employees alone with a bare raises(Exception), which
    green-lit an uncaught HPKE error 500ing the whole run and naming nobody.
    Now: `execute_pay_run` must raise PayRunBlocked (the 422 family the API
    already maps) NAMING the employee, with no plaintext and no ciphertext,
    and nothing reaching the provider."""
    from usali.payroll_run import PayRunBlocked, execute_pay_run

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    spliced = _sealed_to(_OPENER, emp_id, account_slot(2, False), b"999999999")
    _chain_row(db_session, emp_id, 1, sealed_account=spliced)
    db_session.commit()

    provider = InMemoryPayrollProvider()
    with pytest.raises(PayRunBlocked) as exc_info:
        execute_pay_run(db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR,
                        provider=provider, provider_name="memory",
                        opener=_OPENER, actor="pa")
    db_session.rollback()
    message = str(exc_info.value)
    assert "Hank H" in message and str(emp_id) in message
    assert "999999999" not in message
    assert spliced not in message
    assert provider._employees == [], "nothing may reach the provider"


# --- the aad slot strings are a CONTRACT, pinned as literals -----------------


def test_slot_derivation_is_the_literal_contract_string():
    """The E5 review's finding: every slot test sealed through the same
    helper the open path uses, so seal and open agreed BY CONSTRUCTION and a
    typo'd slot survived the whole suite. These literals are the contract
    with (a) every pre-E5 envelope in production (legacy) and (b) the
    frontend's depositAad (new) — change them and real ciphertext stops
    opening, so they are pinned as strings, not through the helper."""
    assert account_slot(1, False) == "deposit:1:account"
    assert routing_slot(1, False) == "deposit:1:routing"
    assert account_slot(3, False) == "deposit:3:account"
    assert account_slot(1, True) == "bank_account"
    assert routing_slot(7, True) == "bank_routing"  # ordinal-independent


def test_a_real_pre_e5_envelope_opens_through_sync(db_session):
    """Sealed under the LITERAL legacy aad — `f"{emp}:bank_account"`, written
    out, NOT via account_slot — exactly as every pre-E5 browser sealed it.
    This is the test the plan promised ('build the envelope with the OLD aad
    in the test'); the helper-based version proved only self-consistency."""
    from usali.models import DepositAccount as DA
    from usali.opener import seal_for_test

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    db_session.add(DA(
        employee_id=emp_id, ordinal=1, allocation_type="remainder",
        allocation_value=None, account_type="checking",
        sealed_account=seal_for_test(
            _OPENER.public_key(), b"444000444",
            aad=f"{emp_id}:bank_account".encode()).to_json(),
        sealed_routing=seal_for_test(
            _OPENER.public_key(), b"021000021",
            aad=f"{emp_id}:bank_routing".encode()).to_json(),
        legacy_sealed=True,
    ))
    db_session.commit()
    provider = InMemoryPayrollProvider()
    sync_employees(db_session, provider, _OPENER,
                   provider_name="memory", employee_ids=[emp_id])
    [sent] = provider._employees
    assert sent.deposit_accounts[0].account == "444000444"


def test_a_frontend_shaped_envelope_opens_through_sync(db_session):
    """The other half of the round-trip vector: sealed under the LITERAL
    string the frontend's depositAad produces — `f"{emp}:deposit:1:account"`
    written out — and opened by the backend derivation. Frontend vitest pins
    the same literal from its side."""
    from usali.models import DepositAccount as DA
    from usali.opener import seal_for_test

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    db_session.add(DA(
        employee_id=emp_id, ordinal=1, allocation_type="remainder",
        allocation_value=None, account_type="checking",
        sealed_account=seal_for_test(
            _OPENER.public_key(), b"555000555",
            aad=f"{emp_id}:deposit:1:account".encode()).to_json(),
        sealed_routing=seal_for_test(
            _OPENER.public_key(), b"021000021",
            aad=f"{emp_id}:deposit:1:routing".encode()).to_json(),
        legacy_sealed=False,
    ))
    db_session.commit()
    provider = InMemoryPayrollProvider()
    sync_employees(db_session, provider, _OPENER,
                   provider_name="memory", employee_ids=[emp_id])
    [sent] = provider._employees
    assert sent.deposit_accounts[0].account == "555000555"


def test_gapped_ordinals_open_under_their_own_ordinals(db_session):
    """Ordinals (3, 7) — reachable only by raw SQL, but the aad must derive
    from r.ordinal, never array position: an enumerate()-based derivation
    passes every contiguous-chain test and fails exactly here (the review's
    surviving mutant M11)."""
    from usali.models import DepositAccount as DA
    from usali.opener import seal_for_test

    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    for ordinal, kind, value, acct in (
        (3, "amount", Decimal("25.00"), b"666000666"),
        (7, "remainder", None, b"777000777"),
    ):
        db_session.add(DA(
            employee_id=emp_id, ordinal=ordinal, allocation_type=kind,
            allocation_value=value, account_type="checking",
            sealed_account=seal_for_test(
                _OPENER.public_key(), acct,
                aad=f"{emp_id}:deposit:{ordinal}:account".encode()).to_json(),
            sealed_routing=seal_for_test(
                _OPENER.public_key(), b"021000021",
                aad=f"{emp_id}:deposit:{ordinal}:routing".encode()).to_json(),
            legacy_sealed=False,
        ))
    db_session.commit()
    provider = InMemoryPayrollProvider()
    sync_employees(db_session, provider, _OPENER,
                   provider_name="memory", employee_ids=[emp_id])
    [sent] = provider._employees
    assert [(a.ordinal, a.account) for a in sent.deposit_accounts] == [
        (3, "666000666"), (7, "777000777"),
    ]


# --- the review's money findings, pinned -------------------------------------


def test_preflight_names_a_typeless_backfilled_account(db_session):
    """A backfilled row whose pre-E5 profile never stated checking/savings
    carries NULL — the backfill must not invent an ACH account class. Named,
    not guessed."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    from usali.models import DepositAccount as DA

    db_session.add(DA(
        employee_id=emp_id, ordinal=1, allocation_type="remainder",
        allocation_value=None, account_type=None,
        sealed_account=_sealed_to(_OPENER, emp_id, account_slot(1, True), b"1"),
        sealed_routing=_sealed_to(_OPENER, emp_id, routing_slot(1, True), b"2"),
        legacy_sealed=True,
    ))
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "account type" in named[0]


def test_profile_and_chain_problems_report_together(db_session):
    """Serial discovery, closed: an employee missing BOTH the ssn and a legal
    chain learns both in ONE preflight, not one per run."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Hank H",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()  # NO profile at all
    _shift(db_session, device_id, emp.employee_id, 6, 8, 16)
    db_session.commit()
    _approved_card(db_session, emp.employee_id)
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    text = " ".join(report.problems)
    assert "ssn" in text and "deposit" in text


def test_fixed_amounts_exceeding_gross_are_a_named_blocker(db_session):
    """8h at $20 is $160 gross; a $5,000,000 fixed carve starves the
    remainder and the provider's handling is undefined — it must not reach
    them (previously nobody's named problem)."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1, allocation_type="amount",
               allocation_value=Decimal("5000000.00"))
    _chain_row(db_session, emp_id, 2)
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "gross" in named[0]


def test_a_chain_replaced_after_provider_sync_blocks_by_name(db_session):
    """The provider record is created ONCE; the port has no update call, so a
    chain edit after first sync means the provider routes money by a
    destination the employee replaced — while preflight green-lit the DB's
    new chain (active false assurance, per the review). Blocks by name until
    the port grows an update path."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    db_session.commit()
    provider = InMemoryPayrollProvider()
    sync_employees(db_session, provider, _OPENER,
                   provider_name="memory", employee_ids=[emp_id])
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert report.ok, report.problems  # unchanged chain: no blocker

    from usali.models import DepositAccount as DA

    db_session.execute(
        DA.__table__.delete().where(DA.employee_id == emp_id)
    )
    db_session.commit()
    import time as _time

    _time.sleep(0.05)  # server clocks: the new row must be strictly newer
    _chain_row(db_session, emp_id, 1, account=b"888000888")
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "provider" in named[0] and "888000888" not in named[0]


# --- the port itself never leaks ---------------------------------------------


def test_the_plain_account_repr_is_redacted(db_session):
    acct = PlainDepositAccount(
        ordinal=1, allocation_type="remainder", allocation_value=None,
        account_type="checking", routing="021000021", account="000123456",
    )
    assert "000123456" not in repr(acct) and "021000021" not in repr(acct)
    assert "000123456" not in str(acct)
