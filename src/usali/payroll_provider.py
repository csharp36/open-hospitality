"""The provider-agnostic payroll port (Pillar C2).

Canonical types the whole app speaks; each adapter maps them to one provider's
real API shape. The port is deliberately narrow: sync an employee, submit a pay
run, fetch its result. Money is Decimal dollars everywhere — adapters convert
provider units (e.g. ADP cents) at the boundary.

SECURITY: `PayrollEmployee` carries TRANSIENT plaintext PII (opened from the
sealed vault at send time). It must never be persisted, logged, or echoed into
an exception message.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

_OT = Decimal("1.5")
_DT = Decimal("2")
_CENTS = Decimal("0.01")


class ProviderError(Exception):
    """A provider call failed. Messages must NEVER include request payloads."""


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_field_encryption: bool
    # G1: both default False so a provider that has not DECLARED a
    # capability does not have it — absence degrades to the named blocker
    # (loudly wrong), never to silent staleness or a silently-dropped
    # figure.
    supports_employee_update: bool = False
    supports_sick_balance_display: bool = False


@dataclass(frozen=True)
class PlainDepositAccount:
    """ONE opened deposit destination. Exists only inside the provider-send
    path (`sync_employees` opens it, the adapter serializes it, it dies with
    the call) — never stored, never logged."""

    ordinal: int
    allocation_type: str  # amount | percent | remainder
    allocation_value: Decimal | None
    account_type: str | None
    routing: str
    account: str

    def __repr__(self) -> str:  # defense in depth: never leak PII via repr/logs
        return f"PlainDepositAccount(ordinal={self.ordinal}, ...redacted)"


@dataclass(frozen=True)
class PayrollEmployee:
    full_name: str
    ssn: str
    # In ordinal order; E5 — the deposit destination is a chain, and the
    # allocation travels with each account so the provider can route net pay.
    deposit_accounts: tuple[PlainDepositAccount, ...]
    tax_elections: str | None

    def __repr__(self) -> str:  # defense in depth: never leak PII via repr/logs
        return f"PayrollEmployee(full_name={self.full_name!r}, ...redacted)"


@dataclass(frozen=True)
class PayRunEntry:
    provider_employee_id: str
    regular_hours: Decimal
    ot_hours: Decimal
    dt_hours: Decimal
    hourly_rate: Decimal
    # G1: sick hours are paid at hourly_rate with NO multiplier (G plan
    # decision 5 — the single-rate contract extends to sick days; a sick
    # day priced differently is refused upstream, never averaged in).
    sick_hours: Decimal = Decimal("0")
    # §246(i) display figure: the available balance as of the check date.
    # None = "do not display" — sent only to providers whose capabilities
    # say they can render it on the wage statement (G plan decision 9).
    sick_balance_hours: Decimal | None = None


@dataclass(frozen=True)
class ProviderRun:
    provider_run_id: str
    status: str  # "submitted"


@dataclass(frozen=True)
class PayRunResultLine:
    provider_employee_id: str
    gross: Decimal
    employee_taxes: Decimal
    employer_taxes: Decimal
    net: Decimal


@dataclass(frozen=True)
class PayRunResult:
    status: str  # "submitted" | "processed"
    lines: list[PayRunResultLine]


class PayrollProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def sync_employee(self, employee: PayrollEmployee) -> str: ...
    def update_employee(
        self, provider_employee_id: str, employee: PayrollEmployee
    ) -> None:
        """Full-replace of the provider-side record (G plan decision 1).
        Callers must consult capabilities().supports_employee_update first;
        an unsupporting provider raises ProviderError."""
        ...
    def submit_pay_run(
        self, *, period_start: date, period_end: date, check_date: date,
        entries: list[PayRunEntry],
    ) -> ProviderRun: ...
    def get_pay_run(self, provider_run_id: str) -> PayRunResult: ...


def _gross(entry: PayRunEntry) -> Decimal:
    return (
        entry.regular_hours * entry.hourly_rate
        + entry.ot_hours * entry.hourly_rate * _OT
        + entry.dt_hours * entry.hourly_rate * _DT
        + entry.sick_hours * entry.hourly_rate
    ).quantize(_CENTS)


@dataclass
class InMemoryPayrollProvider:
    """Test fake: synchronous, deterministic 15% employee / 10% employer taxes."""

    employee_tax_rate: Decimal = Decimal("0.15")
    employer_tax_rate: Decimal = Decimal("0.10")
    # The PII-transit log: every payload SENT (sync AND update), in order —
    # the E5 tests read it as "what reached the provider". Current
    # provider-side STATE lives in _by_id; stored_employees() exposes it.
    _employees: list[PayrollEmployee] = field(default_factory=list)
    _by_id: dict[str, PayrollEmployee] = field(default_factory=dict)
    _runs: dict[str, PayRunResult] = field(default_factory=dict)
    _submitted: dict[str, tuple[PayRunEntry, ...]] = field(default_factory=dict)

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_field_encryption=False,
            supports_employee_update=True,
            supports_sick_balance_display=True,
        )

    def sync_employee(self, employee: PayrollEmployee) -> str:
        self._employees.append(employee)
        provider_employee_id = f"mem-{len(self._employees)}"
        self._by_id[provider_employee_id] = employee
        return provider_employee_id

    def update_employee(
        self, provider_employee_id: str, employee: PayrollEmployee
    ) -> None:
        if provider_employee_id not in self._by_id:
            raise ProviderError(
                f"unknown employee {provider_employee_id}"
            )
        self._employees.append(employee)
        self._by_id[provider_employee_id] = employee

    def stored_employees(self) -> dict[str, PayrollEmployee]:
        """Current provider-side state, id -> latest full-replace payload."""
        return dict(self._by_id)

    def submit_pay_run(
        self, *, period_start: date, period_end: date, check_date: date,
        entries: list[PayRunEntry],
    ) -> ProviderRun:
        lines = []
        for e in entries:
            gross = _gross(e)
            etax = (gross * self.employee_tax_rate).quantize(_CENTS)
            xtax = (gross * self.employer_tax_rate).quantize(_CENTS)
            lines.append(PayRunResultLine(
                provider_employee_id=e.provider_employee_id, gross=gross,
                employee_taxes=etax, employer_taxes=xtax, net=gross - etax,
            ))
        run_id = f"mem-run-{len(self._runs) + 1}"
        self._runs[run_id] = PayRunResult(status="processed", lines=lines)
        self._submitted[run_id] = tuple(entries)
        return ProviderRun(provider_run_id=run_id, status="submitted")

    def submitted_entries(self, provider_run_id: str) -> tuple[PayRunEntry, ...]:
        """What submit_pay_run RECEIVED for a run. The §246(i) display
        figure never moves money, so the result lines cannot prove it
        arrived — the G5 tests read it here. Entries carry no PII."""
        return self._submitted[provider_run_id]

    def get_pay_run(self, provider_run_id: str) -> PayRunResult:
        if provider_run_id not in self._runs:
            raise ProviderError(f"unknown pay run {provider_run_id}")
        return self._runs[provider_run_id]
