"""H7: staleness goes fingerprint-based (decision 8).

`provider_payload_stale` becomes "stored fingerprint != current
fingerprint", where the fingerprint is SHA-256 over EXACTLY the fields
the sync payload ships — full_name, the sealed ssn ciphertext, and the
ordered chain rows' (ordinal, allocation_type, allocation_value,
account_type, sealed_routing, sealed_account) — computed WITHOUT opening
anything. Consequences, each pinned here or in the superseded S9 test:
the tax-elections edit stops re-sending SSN+chain (the G7 PII residual),
the transaction-start timestamp race closes, a NULL fingerprint (every
pre-H ref) self-heals with one re-send, and a re-seal or name edit
honestly re-sends because the payload really did change.
"""

import time

from sqlalchemy import select, update as sql_update

from tests.test_e5_provider_port import _OPENER, _sealed_to
from tests.test_provider_resync import (
    _CountingOpener,
    _synced_world,
)
from usali.models import Employee, EmployeePayrollProfile, ProviderEmployeeRef
from usali.payroll_run import provider_payload_stale, sync_employees


def _resync(db_session, provider, emp_id, opener=None):
    sync_employees(db_session, provider, opener or _OPENER,
                   provider_name="memory", employee_ids=[emp_id])
    db_session.commit()


def test_a_tax_elections_edit_triggers_no_resend_and_no_opens(db_session):
    """THE G7 PII residual, retired: tax elections never ship (no adapter
    serializes them), so an elections-only edit must not push SSN + chain
    plaintext through transit again. The fingerprint covers only shipped
    fields — the edit is invisible to it, the vault stays closed, and
    nothing reaches the provider."""
    emp_id, provider, ref = _synced_world(db_session)
    time.sleep(0.05)
    profile = db_session.execute(
        select(EmployeePayrollProfile).where(
            EmployeePayrollProfile.employee_id == emp_id)
    ).scalar_one()
    profile.tax_elections_sealed = _sealed_to(
        _OPENER, emp_id, "tax_elections", b'{"filing": "single"}'
    )
    db_session.commit()

    assert provider_payload_stale(db_session, emp_id, ref) is False
    counting = _CountingOpener(_OPENER)
    _resync(db_session, provider, emp_id, opener=counting)
    assert counting.opens == 0, "an unshipped edit must not open the vault"
    assert len(provider._employees) == 1  # first sync only — no update


def test_a_null_fingerprint_resends_once_then_settles(db_session):
    """Every pre-H ref (and the H2 migration deliberately backfills none):
    NULL must read STALE — fail toward the extra send, never the skipped
    one — and one successful re-send self-heals it."""
    emp_id, provider, ref = _synced_world(db_session)
    db_session.execute(
        sql_update(ProviderEmployeeRef)
        .where(ProviderEmployeeRef.ref_id == ref.ref_id)
        .values(payload_fingerprint=None)
    )
    db_session.commit()
    db_session.refresh(ref)
    assert provider_payload_stale(db_session, emp_id, ref) is True

    _resync(db_session, provider, emp_id)
    assert len(provider._employees) == 2  # exactly one healing update
    db_session.refresh(ref)
    assert ref.payload_fingerprint is not None

    _resync(db_session, provider, emp_id)
    assert len(provider._employees) == 2  # settled — no further sends


def test_a_reseal_of_the_same_plaintext_resends(db_session):
    """Ciphertext identity IS payload identity: the predicate never opens
    an envelope, so a re-seal of the same SSN is a new payload and
    honestly re-sends — harmless, idempotent, and correct, since a
    re-seal is an edit event (key rotation, re-entry)."""
    emp_id, provider, ref = _synced_world(db_session)
    profile = db_session.execute(
        select(EmployeePayrollProfile).where(
            EmployeePayrollProfile.employee_id == emp_id)
    ).scalar_one()
    profile.ssn_sealed = _sealed_to(_OPENER, emp_id, "ssn", b"123-45-6789")
    db_session.commit()

    assert provider_payload_stale(db_session, emp_id, ref) is True
    _resync(db_session, provider, emp_id)
    assert len(provider._employees) == 2


def test_an_account_type_edit_resends(db_session):
    """Every shipped chain field is in the fingerprint — including the
    plain (non-sealed) ones. account_type is the ACH account class the
    provider deposits by; an edit the fingerprint missed would leave the
    provider depositing into the old class forever."""
    from usali.models import DepositAccount

    emp_id, provider, ref = _synced_world(db_session)
    row = db_session.execute(
        select(DepositAccount).where(DepositAccount.employee_id == emp_id)
    ).scalar_one()
    assert row.account_type != "savings"
    row.account_type = "savings"
    db_session.commit()

    assert provider_payload_stale(db_session, emp_id, ref) is True
    _resync(db_session, provider, emp_id)
    assert len(provider._employees) == 2
    [pid] = {
        r.provider_employee_id
        for r in db_session.execute(select(ProviderEmployeeRef)).scalars()
    }
    assert provider.stored_employees()[pid].deposit_accounts[0].account_type == "savings"


def test_a_name_edit_now_resends(db_session):
    """The old predicate's accepted gap, closed for free: full_name SHIPS
    in the payload, so the fingerprint covers it and a rename re-sends.
    The provider's copy of the name no longer waits for the next
    unrelated edit to correct itself."""
    emp_id, provider, ref = _synced_world(db_session)
    employee = db_session.get(Employee, emp_id)
    assert employee is not None
    employee.full_name = "Hank Hyphenated-Now"
    db_session.commit()

    assert provider_payload_stale(db_session, emp_id, ref) is True
    _resync(db_session, provider, emp_id)
    [pid] = {
        r.provider_employee_id
        for r in db_session.execute(select(ProviderEmployeeRef)).scalars()
    }
    assert provider.stored_employees()[pid].full_name == "Hank Hyphenated-Now"
    # ...and settles: same content, no second send.
    _resync(db_session, provider, emp_id)
    assert len(provider._employees) == 2
