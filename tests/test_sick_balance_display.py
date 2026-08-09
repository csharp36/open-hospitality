"""G5: the §246(i) balance display — the ledger fold as of the CHECK DATE
rides each submitted entry, sent only to a provider whose capabilities say
the stub can render it.

§246(i) pins the notice to "the designated pay date with the employee's
payment of wages" (docs/reference/ca-sick-leave.md §4b), so the figure is
the balance as of the check date — not period end, and not "today". None on
the wire means "do not display": a provider that has not declared the
capability gets None, never a figure it would silently drop (both real
adapters keep refusing a balance-carrying entry until the go-live sandbox
verifies them — tests/test_provider_contract.py).
"""

from datetime import date
from decimal import Decimal

from tests.test_e5_provider_port import _OPENER, _chain_row, _payable_employee
from tests.test_payroll_run import _seed
from tests.test_sick_pay_submission import _assemble, _entry_for, _sick
from usali.payroll_provider import InMemoryPayrollProvider, ProviderCapabilities
from usali.payroll_run import execute_pay_run

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)   # period 2026-07-06 .. 2026-07-19
_SICK_DAY = date(2026, 7, 8)
# check date = period_end + 5 (the _seed schedule) = 2026-07-24


class _NoBalanceDisplayProvider(InMemoryPayrollProvider):
    """Updatable, but has NOT declared it can render an external balance."""

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_field_encryption=False,
            supports_employee_update=True,
        )


def test_each_entry_carries_its_own_check_date_balance(db_session):
    """Two employees, two different folds — the figure is resolved per
    employee, not sampled once for the run. Hank's PAID usage this period
    is already off his displayed balance (the stub must not promise hours
    this very check spends)."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "12.00", date(2026, 7, 1), entry_type="accrual")
    _sick(db_session, hank, "8.00", _SICK_DAY)
    wanda = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, wanda, 1)
    _sick(db_session, wanda, "40.00", date(2026, 7, 1), entry_type="accrual")

    report = _assemble(db_session)
    assert report.ok, report.problems
    assert _entry_for(report, hank).sick_balance_hours == Decimal("4.00")
    assert _entry_for(report, wanda).sick_balance_hours == Decimal("40.00")


def test_the_balance_is_as_of_the_check_date(db_session):
    """An accrual dated after period end but on/before the check date is IN
    the displayed figure; one dated after the check date is not. Folding at
    period end would understate what §246(i) shows on the pay date."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "10.00", date(2026, 7, 21), entry_type="accrual")
    _sick(db_session, hank, "5.00", date(2026, 7, 25), entry_type="accrual")

    report = _assemble(db_session)
    assert report.ok, report.problems
    assert _entry_for(report, hank).sick_balance_hours == Decimal("10.00")


def test_a_zero_balance_still_displays_as_zero(db_session):
    """None means 'do not display'. §246(i) requires the figure even when
    it is zero — an empty ledger displays 0.00, it does not disappear."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)

    report = _assemble(db_session)
    assert report.ok, report.problems
    assert _entry_for(report, hank).sick_balance_hours == Decimal("0.00")


def test_the_balance_reaches_the_provider_and_moves_no_money(db_session):
    """End to end: the submitted entry carries the fold, and the provider's
    gross is unchanged by it — §246(i) is a display figure, not pay."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "12.00", date(2026, 7, 1), entry_type="accrual")
    _sick(db_session, hank, "8.00", _SICK_DAY)
    provider = InMemoryPayrollProvider()

    run = execute_pay_run(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR, provider=provider,
        provider_name="memory", opener=_OPENER, actor="test",
    )
    db_session.commit()
    (entry,) = provider.submitted_entries(run.provider_run_id)
    assert entry.sick_balance_hours == Decimal("4.00")
    assert entry.sick_hours == Decimal("8.00")
    # 8h worked + 8h sick at $20 — the 4.00 display hours add nothing.
    result = provider.get_pay_run(run.provider_run_id)
    assert result.lines[0].gross == Decimal("320.00")


def test_an_undeclared_capability_sends_no_balance(db_session):
    """The gate reads the DECLARED capability, not hope: a provider that
    never said it can render the figure receives None end to end."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "12.00", date(2026, 7, 1), entry_type="accrual")
    provider = _NoBalanceDisplayProvider()

    run = execute_pay_run(
        db_session, "HISJ", _PERIOD_DAY, anchor=_ANCHOR, provider=provider,
        provider_name="memory", opener=_OPENER, actor="test",
    )
    db_session.commit()
    (entry,) = provider.submitted_entries(run.provider_run_id)
    assert entry.sick_balance_hours is None
