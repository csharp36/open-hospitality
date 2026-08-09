from datetime import date
from decimal import Decimal

from usali.payroll_provider import (
    InMemoryPayrollProvider,
    PayrollEmployee,
    PayRunEntry,
)


def _emp(n="Hank H"):
    from usali.payroll_provider import PlainDepositAccount

    return PayrollEmployee(
        full_name=n, ssn="123-45-6789",
        deposit_accounts=(PlainDepositAccount(
            ordinal=1, allocation_type="remainder", allocation_value=None,
            account_type="checking", routing="021000021", account="000123456",
        ),),
        tax_elections=None,
    )


def _entry(pid, rate="20.00", reg="16.00", ot="0.00", dt="0.00"):
    return PayRunEntry(provider_employee_id=pid, regular_hours=Decimal(reg),
                       ot_hours=Decimal(ot), dt_hours=Decimal(dt),
                       hourly_rate=Decimal(rate))


def test_sync_returns_a_stable_id_per_employee():
    p = InMemoryPayrollProvider()
    a = p.sync_employee(_emp())
    assert a and isinstance(a, str)


def test_round_trip_produces_processed_results_with_gross():
    p = InMemoryPayrollProvider()
    pid = p.sync_employee(_emp())
    run = p.submit_pay_run(period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
                           check_date=date(2026, 7, 24), entries=[_entry(pid)])
    assert run.provider_run_id
    result = p.get_pay_run(run.provider_run_id)
    assert result.status == "processed"
    line = result.lines[0]
    assert line.provider_employee_id == pid
    assert line.gross == Decimal("320.00")            # 16h x $20
    assert line.net == line.gross - line.employee_taxes
    assert line.employer_taxes > 0


def test_overtime_multipliers_are_priced():
    p = InMemoryPayrollProvider()
    pid = p.sync_employee(_emp())
    run = p.submit_pay_run(period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
                           check_date=date(2026, 7, 24),
                           entries=[_entry(pid, reg="8.00", ot="2.00", dt="1.00")])
    line = p.get_pay_run(run.provider_run_id).lines[0]
    # 8*20 + 2*20*1.5 + 1*20*2 = 160 + 60 + 40 = 260
    assert line.gross == Decimal("260.00")


def test_unknown_run_id_raises():
    p = InMemoryPayrollProvider()
    try:
        p.get_pay_run("nope")
        raise AssertionError("expected ProviderError")
    except Exception as exc:
        assert "nope" in str(exc)


def test_capabilities_flag_exists():
    assert InMemoryPayrollProvider().capabilities().supports_field_encryption is False


def test_repr_and_str_redact_pii():
    emp = _emp()
    for rendered in (repr(emp), str(emp)):
        assert "123-45-6789" not in rendered
        assert "000123456" not in rendered
        assert "021000021" not in rendered
        assert "Hank H" in rendered


# --- G1: employee update + sick hours on the port ----------------------------


def test_update_employee_replaces_the_stored_record():
    """Full-replace, mirroring the vault surface (G plan decision 1): the
    updated payload is the record now; the neighbour is untouched."""
    p = InMemoryPayrollProvider()
    a = p.sync_employee(_emp("Alma A"))
    b = p.sync_employee(_emp("Bert B"))
    p.update_employee(a, _emp("Alma A-Married"))
    stored = {pid: e.full_name for pid, e in p.stored_employees().items()}
    assert stored[a] == "Alma A-Married"
    assert stored[b] == "Bert B"


def test_update_of_an_unknown_id_refuses_without_payload_echo():
    """The employee payload carries transient plaintext PII — a refusal may
    name the provider id, never the payload."""
    import pytest

    from usali.payroll_provider import ProviderError

    p = InMemoryPayrollProvider()
    with pytest.raises(ProviderError) as err:
        p.update_employee("mem-404", _emp())
    assert "mem-404" in str(err.value)
    assert "123-45-6789" not in str(err.value)
    assert "000123456" not in str(err.value)


def test_sick_hours_fold_into_gross_at_straight_time():
    """Sick hours are paid at the entry's single hourly_rate with no
    multiplier (G plan decision 5)."""
    p = InMemoryPayrollProvider()
    pid = p.sync_employee(_emp())
    run = p.submit_pay_run(
        period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
        check_date=date(2026, 7, 24),
        entries=[PayRunEntry(
            provider_employee_id=pid, regular_hours=Decimal("8.00"),
            ot_hours=Decimal("2.00"), dt_hours=Decimal("0.00"),
            hourly_rate=Decimal("20.00"), sick_hours=Decimal("4.00"),
        )],
    )
    line = p.get_pay_run(run.provider_run_id).lines[0]
    # 8*20 + 2*20*1.5 + 4*20 = 160 + 60 + 80 = 300
    assert line.gross == Decimal("300.00")


def test_new_entry_fields_default_so_existing_call_sites_still_describe_reality():
    e = _entry("mem-1")
    assert e.sick_hours == Decimal("0")
    assert e.sick_balance_hours is None


def test_capability_defaults_are_refusal_shaped():
    """A provider that has not declared the new capabilities does not have
    them — False by default, so absence degrades to the named blocker, not
    to silent staleness (G plan decision 4)."""
    from usali.payroll_provider import ProviderCapabilities

    bare = ProviderCapabilities(supports_field_encryption=False)
    assert bare.supports_employee_update is False
    assert bare.supports_sick_balance_display is False
    mem = InMemoryPayrollProvider().capabilities()
    assert mem.supports_employee_update is True
    assert mem.supports_sick_balance_display is True
