"""G2: stale provider payloads re-sync instead of refusing — when they can.

The E5 review's root cause: `sync_employees` creates the provider record
ONCE, so any chain (or SSN) change after first sync could never reach the
provider, and preflight blocked the run by name. G2 gives the port an
update path and ONE staleness predicate (`provider_payload_stale`):

- stale + provider supports update -> full-replace re-sync inside
  `sync_employees`, `synced_at` bumped on success and ONLY on success;
- stale + provider cannot update -> the named blocker stands (preflight,
  and again in sync as defence in depth);
- not stale -> no provider call and, critically, NO sealed field opened —
  re-sync must not widen the PII-transit surface for unchanged people.
"""

import time
from datetime import date

import pytest
from sqlalchemy import select

from tests.test_e5_provider_port import (
    _OPENER,
    _chain_row,
    _payable_employee,
    _sealed_to,
)
from tests.test_payroll_run import _seed
from usali.models import DepositAccount, EmployeePayrollProfile, ProviderEmployeeRef
from usali.payroll_provider import (
    InMemoryPayrollProvider,
    PayrollEmployee,
    ProviderCapabilities,
    ProviderError,
)
from usali.payroll_run import (
    PayRunBlocked,
    assemble_pay_run_entries,
    provider_payload_stale,
    sync_employees,
)

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)


class _CountingOpener:
    """Wraps the test opener; counts every open() — the PII-transit meter."""

    def __init__(self, inner):
        self._inner = inner
        self.opens = 0

    def open(self, envelope, *, aad):
        self.opens += 1
        return self._inner.open(envelope, aad=aad)

    def public_key(self):
        return self._inner.public_key()


class _NoUpdateProvider(InMemoryPayrollProvider):
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_field_encryption=False,
                                    supports_employee_update=False)


class _FailingUpdateProvider(InMemoryPayrollProvider):
    def update_employee(self, provider_employee_id: str,
                        employee: PayrollEmployee) -> None:
        raise ProviderError("gusto employee update failed (503)")


def _synced_world(db_session, provider=None):
    """Hank, complete and payable, already synced once. Returns
    (emp_id, provider, ref_row)."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, emp_id, 1)
    db_session.commit()
    provider = provider or InMemoryPayrollProvider()
    sync_employees(db_session, provider, _OPENER,
                   provider_name="memory", employee_ids=[emp_id])
    db_session.commit()
    ref = db_session.execute(select(ProviderEmployeeRef).where(
        ProviderEmployeeRef.employee_id == emp_id)).scalar_one()
    return emp_id, provider, ref


def _replace_chain(db_session, emp_id, account=b"888000888"):
    db_session.execute(
        DepositAccount.__table__.delete().where(
            DepositAccount.employee_id == emp_id)
    )
    db_session.commit()
    time.sleep(0.05)  # server clocks: the new row must be strictly newer
    _chain_row(db_session, emp_id, 1, account=account)
    db_session.commit()


def test_a_chain_edit_triggers_exactly_one_update_with_the_new_chain(db_session):
    emp_id, provider, ref = _synced_world(db_session)
    before = ref.synced_at
    _replace_chain(db_session, emp_id)

    refs = sync_employees(db_session, provider, _OPENER,
                          provider_name="memory", employee_ids=[emp_id])
    db_session.commit()

    # Exactly one more payload reached the provider, and it carries the NEW
    # account; the provider-side id did not change.
    assert len(provider._employees) == 2
    [pid] = refs.values()
    stored = provider.stored_employees()[pid]
    assert stored.deposit_accounts[0].account == "888000888"
    db_session.refresh(ref)
    assert ref.synced_at > before


def test_a_profile_edit_triggers_resync_with_the_new_ssn(db_session):
    emp_id, provider, ref = _synced_world(db_session)
    time.sleep(0.05)
    profile = db_session.execute(select(EmployeePayrollProfile).where(
        EmployeePayrollProfile.employee_id == emp_id)).scalar_one()
    profile.ssn_sealed = _sealed_to(_OPENER, emp_id, "ssn", b"999-99-9999")
    db_session.commit()

    refs = sync_employees(db_session, provider, _OPENER,
                          provider_name="memory", employee_ids=[emp_id])
    db_session.commit()
    [pid] = refs.values()
    assert provider.stored_employees()[pid].ssn == "999-99-9999"


def test_unchanged_payload_makes_no_call_and_opens_no_pii(db_session):
    emp_id, provider, _ = _synced_world(db_session)
    counting = _CountingOpener(_OPENER)
    sync_employees(db_session, provider, counting,
                   provider_name="memory", employee_ids=[emp_id])
    assert counting.opens == 0, "re-sync must not widen the PII-transit surface"
    assert len(provider._employees) == 1  # first sync only; no update sent


def test_update_failure_refuses_naming_the_employee_and_keeps_synced_at(db_session):
    emp_id, provider, ref = _synced_world(
        db_session, provider=_FailingUpdateProvider())
    before = ref.synced_at
    _replace_chain(db_session, emp_id)

    with pytest.raises(PayRunBlocked) as err:
        sync_employees(db_session, provider, _OPENER,
                       provider_name="memory", employee_ids=[emp_id])
    assert "Hank H" in str(err.value)
    assert "888000888" not in str(err.value)
    # In-SESSION, before any rollback: no pending bump may exist. The
    # blessed path rolls back with the exception, but a caller that
    # catches the blocker and commits anyway must not persist a synced_at
    # the provider never earned — that hides the staleness forever. Same
    # for the fingerprint (H7): stored only on delivery, so the payload
    # must still read STALE after a failed update.
    assert ref.synced_at == before
    from usali.payroll_run import provider_payload_stale

    assert provider_payload_stale(db_session, emp_id, ref) is True
    db_session.rollback()
    db_session.refresh(ref)
    assert ref.synced_at == before
    assert provider_payload_stale(db_session, emp_id, ref) is True


def test_an_unupdatable_provider_still_refuses_a_stale_payload(db_session):
    """Defence in depth: preflight is the named gate, but sync itself must
    also refuse — a caller reaching sync with a stale payload and no update
    capability must never quietly submit against the provider's old copy."""
    emp_id, provider, _ = _synced_world(db_session, provider=_NoUpdateProvider())
    _replace_chain(db_session, emp_id)

    with pytest.raises(PayRunBlocked) as err:
        sync_employees(db_session, provider, _OPENER,
                       provider_name="memory", employee_ids=[emp_id])
    assert "Hank H" in str(err.value)
    assert len(provider._employees) == 1  # nothing further reached it


def test_preflight_blocks_stale_only_when_the_provider_cannot_update(db_session):
    emp_id, provider, _ = _synced_world(db_session)
    _replace_chain(db_session, emp_id)

    # Unknown or update-less provider: the named blocker stands.
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "provider" in named[0]
    report = assemble_pay_run_entries(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=_NoUpdateProvider().capabilities(),
    )
    assert [p for p in report.problems if "Hank H" in p]

    # Update-capable provider: no blocker — sync will re-send the payload.
    report = assemble_pay_run_entries(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )
    assert report.ok, report.problems


def test_a_profile_edit_also_blocks_preflight_for_an_unupdatable_provider(db_session):
    """The E5 record said 'chain (or SSN) change' — the blocker must cover
    the profile half too, via the same predicate."""
    emp_id, provider, _ = _synced_world(db_session)
    time.sleep(0.05)
    profile = db_session.execute(select(EmployeePayrollProfile).where(
        EmployeePayrollProfile.employee_id == emp_id)).scalar_one()
    profile.ssn_sealed = _sealed_to(_OPENER, emp_id, "ssn", b"999-99-9999")
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert [p for p in report.problems if "Hank H" in p]


def test_the_predicate_is_fresh_after_first_sync(db_session):
    """First sync happens AFTER the chain and profile exist, so a
    just-synced employee is not stale — the predicate's floor case."""
    emp_id, _, ref = _synced_world(db_session)
    assert provider_payload_stale(db_session, emp_id, ref) is False
