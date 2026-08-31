"""The provider contract: every adapter must pass this suite unchanged.

Parametrized over (adapter factory, expected employee taxes, expected employer
taxes) triples wired through SyncASGITransport — in-process, offline, no ports.
The expected tax columns pin each mock's deterministic rates EXACTLY (canonical
gross 260.00: gusto 15%/10% -> 39.00/26.00; adp 18%/11% -> 46.80/28.60) so a
wrong-but-positive field mapping cannot pass. Adding a provider = adding a
param row here; every provider still runs the identical assertions.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from usali.adp_adapter import AdpAdapter
from usali.adp_mock import create_mock_adp
from usali.gusto_adapter import GustoAdapter
from usali.gusto_mock import create_mock_gusto
from usali.payroll_provider import (
    PlainDepositAccount,
    PayrollEmployee,
    PayrollProvider,
    PayRunEntry,
    ProviderError,
)
from usali.qbo_client import SyncASGITransport


def _gusto() -> PayrollProvider:
    return GustoAdapter(base_url="http://mock-gusto", api_token="mock",
                        company_id="mock", transport=SyncASGITransport(create_mock_gusto()))


def _adp() -> PayrollProvider:
    return AdpAdapter(base_url="http://mock-adp", client_id="mock", client_secret="mock",
                      transport=SyncASGITransport(create_mock_adp()))


PROVIDERS = [
    pytest.param(_gusto, Decimal("39.00"), Decimal("26.00"), id="gusto"),
    pytest.param(_adp, Decimal("46.80"), Decimal("28.60"), id="adp"),
]


def _emp(full_name: str = "Hank H") -> PayrollEmployee:
    from usali.payroll_provider import PlainDepositAccount

    return PayrollEmployee(
        full_name=full_name, ssn="123-45-6789",
        deposit_accounts=(PlainDepositAccount(
            ordinal=1, allocation_type="remainder", allocation_value=None,
            account_type="checking", routing="021000021", account="000123456",
        ),),
        tax_elections=None,
    )


# The fixture parametrizes on the factory column only (fixtures take a single
# value per param); the tax columns are consumed by the round-trip test below.
_FACTORIES = [pytest.param(p.values[0], id=p.id) for p in PROVIDERS]


@pytest.fixture(params=_FACTORIES)
def provider(request: pytest.FixtureRequest) -> PayrollProvider:
    factory: object = request.param
    assert callable(factory)
    result: PayrollProvider = factory()
    return result


def test_sync_employee_returns_an_id(provider: PayrollProvider) -> None:
    assert provider.sync_employee(_emp())


@pytest.mark.parametrize(
    ("factory", "expected_employee_taxes", "expected_employer_taxes"), PROVIDERS
)
def test_full_round_trip_prices_regular_and_overtime(
    factory: object,
    expected_employee_taxes: Decimal,
    expected_employer_taxes: Decimal,
) -> None:
    assert callable(factory)
    provider: PayrollProvider = factory()
    pid = provider.sync_employee(_emp())
    run = provider.submit_pay_run(
        period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
        check_date=date(2026, 7, 24),
        entries=[PayRunEntry(provider_employee_id=pid, regular_hours=Decimal("8.00"),
                             ot_hours=Decimal("2.00"), dt_hours=Decimal("1.00"),
                             hourly_rate=Decimal("20.00"))],
    )
    assert run.provider_run_id and run.status == "submitted"
    result = provider.get_pay_run(run.provider_run_id)
    assert result.status == "processed"
    line = result.lines[0]
    assert line.provider_employee_id == pid
    assert line.gross == Decimal("260.00")           # 8*20 + 2*30 + 1*40
    # Exact per-provider pins: a wrong-but-positive tax mapping must FAIL here
    # (employer_taxes becomes usali_actual_labor_fact.employer_burden).
    assert line.employee_taxes == expected_employee_taxes
    assert line.employer_taxes == expected_employer_taxes
    assert line.net == line.gross - line.employee_taxes
    # Canonical money is Decimal dollars regardless of the provider's wire unit.
    assert isinstance(line.gross, Decimal)


def test_unknown_run_raises_provider_error_without_payload_echo(
    provider: PayrollProvider,
) -> None:
    with pytest.raises(ProviderError) as exc:
        provider.get_pay_run("does-not-exist")
    assert "123-45-6789" not in str(exc.value)


def test_error_from_sync_never_echoes_pii(provider: PayrollProvider) -> None:
    # Real SSN + real bank data, EMPTY name: the mock 422s on the missing name,
    # so the request payload (which carries the PII) is what must not leak.
    bad = _emp(full_name="")
    with pytest.raises(ProviderError) as exc:
        provider.sync_employee(bad)
    assert "123-45-6789" not in str(exc.value)
    assert "000123456" not in str(exc.value)


# --- badly-behaved providers: sync errors that ECHO the request back ---------
#
# The well-behaved mocks above return field NAMES only, so they cannot catch an
# adapter that folds the provider's response detail into the ProviderError
# message. A REAL provider's validation error can echo the offending value
# (the plaintext SSN/bank the sync request carried) back in its response body.
# These inline apps simulate that, and the adapters must keep it out of the
# exception message — status code only.


def _echoing_gusto() -> PayrollProvider:
    app = FastAPI()

    @app.post("/v1/companies/{company_id}/employees")
    async def create_employee(company_id: str, request: Request) -> JSONResponse:
        body: dict[str, Any] = await request.json()
        acct = body["deposit_accounts"][0]
        return JSONResponse(status_code=422, content={
            "detail": (
                f"invalid ssn {body['ssn']}; account {acct['bank_account']} "
                f"routing {acct['bank_routing']} rejected"
            ),
        })

    return GustoAdapter(base_url="http://echo-gusto", api_token="mock",
                        company_id="mock", transport=SyncASGITransport(app))


def _echoing_adp() -> PayrollProvider:
    app = FastAPI()

    @app.post("/auth/oauth/v2/token")
    async def token() -> dict[str, str]:
        return {"access_token": "tok"}

    @app.post("/hr/v1/workers")
    async def create_worker(request: Request) -> JSONResponse:
        body: dict[str, Any] = await request.json()
        worker = body["worker"]
        deposit = worker["depositAccounts"][0]
        return JSONResponse(status_code=422, content={
            "detail": (
                f"invalid governmentID {worker['governmentID']}; "
                f"account {deposit['accountNumber']} "
                f"routing {deposit['routingNumber']} rejected"
            ),
        })

    return AdpAdapter(base_url="http://echo-adp", client_id="mock", client_secret="mock",
                      transport=SyncASGITransport(app))


ECHOING_PROVIDERS = [
    pytest.param(_echoing_gusto, id="gusto"),
    pytest.param(_echoing_adp, id="adp"),
]


@pytest.mark.parametrize("factory", ECHOING_PROVIDERS)
def test_sync_error_from_an_echoing_provider_never_carries_pii(
    factory: object,
) -> None:
    assert callable(factory)
    provider: PayrollProvider = factory()
    with pytest.raises(ProviderError) as exc:
        provider.sync_employee(_emp())
    message = str(exc.value)
    # The status code survives (operators need it) — the echoed values do NOT.
    assert "422" in message
    assert "123-45-6789" not in message
    assert "000123456" not in message
    assert "021000021" not in message


# --- E5: the split's serialization IS the money routing — pinned exactly -----
#
# The E5 review mutated both adapters to send allocation_value=None and to
# REVERSE deposit priority, and the whole suite stayed green: nothing ever
# sent a valued multi-account chain through either adapter, and the mocks
# validate presence only. The provider computes the split, so the payload is
# the routing — these tests capture the request body and pin it field for
# field, order included.

_CHAIN = (
    PlainDepositAccount(ordinal=1, allocation_type="amount",
                        allocation_value=Decimal("50.00"),
                        account_type="savings",
                        routing="021000021", account="111000111"),
    PlainDepositAccount(ordinal=2, allocation_type="percent",
                        allocation_value=Decimal("25.00"),
                        account_type="checking",
                        routing="121000248", account="222000222"),
    PlainDepositAccount(ordinal=3, allocation_type="remainder",
                        allocation_value=None, account_type="checking",
                        routing="321070007", account="333000333"),
)


def _chain_emp() -> PayrollEmployee:
    return PayrollEmployee(full_name="Hank H", ssn="123-45-6789",
                           deposit_accounts=_CHAIN, tax_elections=None)


def test_gusto_serializes_the_chain_exactly() -> None:
    captured: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/v1/companies/{company_id}/employees")
    async def create_employee(company_id: str, request: Request) -> JSONResponse:
        captured.append(await request.json())
        return JSONResponse(status_code=201, content={"uuid": "g-1"})

    adapter = GustoAdapter(base_url="http://cap-gusto", api_token="mock",
                           company_id="mock", transport=SyncASGITransport(app))
    assert adapter.sync_employee(_chain_emp()) == "g-1"
    [body] = captured
    assert body["deposit_accounts"] == [
        {"bank_routing": "021000021", "bank_account": "111000111",
         "account_type": "savings", "allocation_type": "amount",
         "allocation_value": "50.00"},
        {"bank_routing": "121000248", "bank_account": "222000222",
         "account_type": "checking", "allocation_type": "percent",
         "allocation_value": "25.00"},
        {"bank_routing": "321070007", "bank_account": "333000333",
         "account_type": "checking", "allocation_type": "remainder",
         "allocation_value": None},
    ], "order IS deposit priority; values are 2-dp strings; remainder is null"


def test_adp_serializes_the_chain_exactly() -> None:
    captured: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/auth/oauth/v2/token")
    async def token() -> dict[str, str]:
        return {"access_token": "tok"}

    @app.post("/hr/v1/workers")
    async def create_worker(request: Request) -> JSONResponse:
        captured.append(await request.json())
        return JSONResponse(status_code=201,
                            content={"worker": {"associateOID": "a-1"}})

    adapter = AdpAdapter(base_url="http://cap-adp", client_id="id",
                         client_secret="secret",
                         transport=SyncASGITransport(app))
    assert adapter.sync_employee(_chain_emp()) == "a-1"
    [body] = captured
    assert body["worker"]["depositAccounts"] == [
        {"routingNumber": "021000021", "accountNumber": "111000111",
         "accountType": "savings", "allocationType": "amount",
         "allocationValue": "50.00"},
        {"routingNumber": "121000248", "accountNumber": "222000222",
         "accountType": "checking", "allocationType": "percent",
         "allocationValue": "25.00"},
        {"routingNumber": "321070007", "accountNumber": "333000333",
         "accountType": "checking", "allocationType": "remainder",
         "allocationValue": None},
    ]


# --- G4: employee update + sick hours, implemented for real ------------------


def test_update_employee_round_trips(provider: PayrollProvider) -> None:
    """Both real adapters now support full-replace update (G plan decision
    1); the capability says so and the call succeeds against the mock."""
    assert provider.capabilities().supports_employee_update is True
    pid = provider.sync_employee(_emp())
    provider.update_employee(pid, _emp("Renamed R"))  # no error = delivered


def test_update_of_an_unknown_id_refuses_without_pii_echo(
    provider: PayrollProvider,
) -> None:
    """The update payload carries transient plaintext PII, so the refusal
    is the sync-path fixed message: status + text, never the body."""
    provider.sync_employee(_emp())
    with pytest.raises(ProviderError) as err:
        provider.update_employee("no-such-id", _emp())
    assert "123-45-6789" not in str(err.value)
    assert "000123456" not in str(err.value)


_SICK_GROSS_TAXES = {
    # 8h reg + 4h sick at $20 = $240 gross. gusto 15%/10%; adp 18%/11%.
    "gusto": (Decimal("36.00"), Decimal("24.00")),
    "adp": (Decimal("43.20"), Decimal("26.40")),
}


def test_sick_hours_are_priced_at_straight_time_by_both_mocks(
    provider: PayrollProvider, request: pytest.FixtureRequest,
) -> None:
    pid = provider.sync_employee(_emp())
    run = provider.submit_pay_run(
        period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
        check_date=date(2026, 7, 24),
        entries=[PayRunEntry(
            provider_employee_id=pid, regular_hours=Decimal("8.00"),
            ot_hours=Decimal("0.00"), dt_hours=Decimal("0.00"),
            hourly_rate=Decimal("20.00"), sick_hours=Decimal("4.00"),
        )],
    )
    line = provider.get_pay_run(run.provider_run_id).lines[0]
    assert line.gross == Decimal("240.00")  # 160 worked + 80 sick, 1.0x
    etax, xtax = _SICK_GROSS_TAXES[request.node.callspec.id]
    assert line.employee_taxes == etax
    assert line.employer_taxes == xtax


def test_sick_balance_still_refuses_until_sandbox_verification(
    provider: PayrollProvider,
) -> None:
    """G5 produces `sick_balance_hours` upstream, but whether the REAL
    providers render an externally-tracked balance is unverified — both
    real adapters declare the capability False (assemble then sends None)
    and an entry carrying a figure anyway must refuse, not silently drop
    the §246(i) display. Enablement is the go-live sandbox item."""
    assert provider.capabilities().supports_sick_balance_display is False
    pid = provider.sync_employee(_emp())
    entry = PayRunEntry(
        provider_employee_id=pid, regular_hours=Decimal("8.00"),
        ot_hours=Decimal("0.00"), dt_hours=Decimal("0.00"),
        hourly_rate=Decimal("20.00"), sick_balance_hours=Decimal("12.00"),
    )
    with pytest.raises(ProviderError, match="balance"):
        provider.submit_pay_run(
            period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
            check_date=date(2026, 7, 24), entries=[entry],
        )


def test_gusto_update_serializes_the_full_replace_exactly() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    app = FastAPI()

    @app.put("/v1/companies/{company_id}/employees/{uuid}")
    async def update_employee(
        company_id: str, uuid: str, request: Request
    ) -> JSONResponse:
        captured.append((uuid, await request.json()))
        return JSONResponse(status_code=200, content={"uuid": uuid})

    adapter = GustoAdapter(base_url="http://cap-gusto", api_token="mock",
                           company_id="mock", transport=SyncASGITransport(app))
    adapter.update_employee("g-77", _chain_emp())
    [(uuid, body)] = captured
    assert uuid == "g-77"
    assert body["full_name"] == "Hank H"
    assert body["ssn"] == "123-45-6789"
    assert body["deposit_accounts"] == [
        {"bank_routing": "021000021", "bank_account": "111000111",
         "account_type": "savings", "allocation_type": "amount",
         "allocation_value": "50.00"},
        {"bank_routing": "121000248", "bank_account": "222000222",
         "account_type": "checking", "allocation_type": "percent",
         "allocation_value": "25.00"},
        {"bank_routing": "321070007", "bank_account": "333000333",
         "account_type": "checking", "allocation_type": "remainder",
         "allocation_value": None},
    ], "the update is the SAME payload shape as first sync — full replace"


def test_adp_update_serializes_the_full_replace_exactly() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []
    app = FastAPI()

    @app.post("/auth/oauth/v2/token")
    async def token() -> dict[str, str]:
        return {"access_token": "tok"}

    @app.put("/hr/v1/workers/{oid}")
    async def update_worker(oid: str, request: Request) -> JSONResponse:
        captured.append((oid, await request.json()))
        return JSONResponse(status_code=200,
                            content={"worker": {"associateOID": oid}})

    adapter = AdpAdapter(base_url="http://cap-adp", client_id="id",
                         client_secret="secret",
                         transport=SyncASGITransport(app))
    adapter.update_employee("a-77", _chain_emp())
    [(oid, body)] = captured
    assert oid == "a-77"
    assert body["worker"]["legalName"] == "Hank H"
    assert body["worker"]["governmentID"] == "123-45-6789"
    assert body["worker"]["depositAccounts"] == [
        {"routingNumber": "021000021", "accountNumber": "111000111",
         "accountType": "savings", "allocationType": "amount",
         "allocationValue": "50.00"},
        {"routingNumber": "121000248", "accountNumber": "222000222",
         "accountType": "checking", "allocationType": "percent",
         "allocationValue": "25.00"},
        {"routingNumber": "321070007", "accountNumber": "333000333",
         "accountType": "checking", "allocationType": "remainder",
         "allocationValue": None},
    ]


def test_gusto_submit_serializes_sick_hours_exactly() -> None:
    captured: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/v1/companies/{company_id}/payrolls")
    async def submit(company_id: str, request: Request) -> JSONResponse:
        captured.append(await request.json())
        return JSONResponse(status_code=201, content={
            "payroll_uuid": "pr-1", "status": "submitted"})

    adapter = GustoAdapter(base_url="http://cap-gusto", api_token="mock",
                           company_id="mock", transport=SyncASGITransport(app))
    adapter.submit_pay_run(
        period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
        check_date=date(2026, 7, 24),
        entries=[PayRunEntry(
            provider_employee_id="g-1", regular_hours=Decimal("8.00"),
            ot_hours=Decimal("2.00"), dt_hours=Decimal("0.00"),
            hourly_rate=Decimal("20.00"), sick_hours=Decimal("4.00"),
        )],
    )
    [body] = captured
    assert body["entries"] == [{
        "employee_uuid": "g-1", "regular_hours": "8.00",
        "overtime_hours": "2.00", "double_overtime_hours": "0.00",
        "hourly_rate": "20.00", "sick_hours": "4.00",
    }], "sick hours are their OWN bucket — never folded into regular"


def test_adp_submit_serializes_sick_hours_exactly() -> None:
    captured: list[dict[str, Any]] = []
    app = FastAPI()

    @app.post("/auth/oauth/v2/token")
    async def token() -> dict[str, str]:
        return {"access_token": "tok"}

    @app.post("/payroll/v1/payroll-runs")
    async def submit(request: Request) -> JSONResponse:
        captured.append(await request.json())
        return JSONResponse(status_code=201, content={"payrollRun": {
            "payrollRunID": "r-1", "processingStatus": "SUBMITTED"}})

    adapter = AdpAdapter(base_url="http://cap-adp", client_id="id",
                         client_secret="secret",
                         transport=SyncASGITransport(app))
    adapter.submit_pay_run(
        period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
        check_date=date(2026, 7, 24),
        entries=[PayRunEntry(
            provider_employee_id="a-1", regular_hours=Decimal("8.00"),
            ot_hours=Decimal("2.00"), dt_hours=Decimal("0.00"),
            hourly_rate=Decimal("20.00"), sick_hours=Decimal("4.00"),
        )],
    )
    [body] = captured
    assert body["payrollRun"]["earnings"] == [{
        "associateOID": "a-1", "regularHours": 8.0,
        "overtimeHours": 2.0, "doubleTimeHours": 0.0,
        "hourlyRateInCents": 2000, "sickHours": 4.0,
    }]


def test_a_fractional_rate_survives_the_wire(provider: PayrollProvider) -> None:
    """G7 (tests S11): every rate in the suite was whole-dollar, so an ADP
    serialization truncating cents (int(rate) * 100) survived — anyone
    earning $20.57/h would be submitted at $20.00, every hour of every
    run. 8h at $20.57 must gross exactly $164.56 through both wires."""
    pid = provider.sync_employee(_emp())
    run = provider.submit_pay_run(
        period_start=date(2026, 7, 6), period_end=date(2026, 7, 19),
        check_date=date(2026, 7, 24),
        entries=[PayRunEntry(
            provider_employee_id=pid, regular_hours=Decimal("8.00"),
            ot_hours=Decimal("0.00"), dt_hours=Decimal("0.00"),
            hourly_rate=Decimal("20.57"),
        )],
    )
    line = provider.get_pay_run(run.provider_run_id).lines[0]
    assert line.gross == Decimal("164.56")


# --- D-OH17.8: verify() proves a credential BEFORE the connect endpoint stores it


class _RecordingTransport(httpx.BaseTransport):
    """Wraps a transport and records (method, path) for every request that
    passes through it. Exists for the read-only assertion below: `verify()`
    runs against a tenant's LIVE payroll account on a button press, so
    "reads nothing, writes nothing" has to be checked at the wire, not
    asserted in a docstring."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        return self._inner.handle_request(request)


@pytest.mark.parametrize(("factory", "expected_employee_taxes", "expected_employer_taxes"),
                         PROVIDERS)
def test_verify_succeeds_against_the_mock(
    factory: object,
    expected_employee_taxes: Decimal,
    expected_employer_taxes: Decimal,
) -> None:
    """D-OH17.8: the connect endpoint refuses a credential that cannot
    authenticate, so a checklist item never reads `done` over an integration
    that 502s on first use. Every adapter therefore owes ONE real, read-only,
    authenticated call. `capabilities()` cannot serve as that proof — it is a
    local declaration that touches no network, and using it would make
    D-OH17.8 quietly false while looking correct.

    The tax columns are unused here; they ride along because this is the
    parametrized contract suite (adding a provider = adding a param row, and
    every provider then runs these assertions for free)."""
    del expected_employee_taxes, expected_employer_taxes
    assert callable(factory)
    provider: PayrollProvider = factory()
    provider.verify()


def test_verify_raises_on_a_bad_gusto_token() -> None:
    """The whole point of D-OH17.8: a typo'd key is a 401 at connect time,
    not a 502 on the tenant's first pay run."""
    bad = GustoAdapter(base_url="http://mock-gusto", api_token="wrong", company_id="mock",
                       transport=SyncASGITransport(create_mock_gusto()))
    with pytest.raises(ProviderError) as denied:
        bad.verify()
    assert "401" in str(denied.value)


def test_verify_raises_on_a_company_the_token_cannot_reach() -> None:
    """BOTH halves of the Gusto credential are checked. A valid token aimed
    at a company id this integration cannot reach is still a broken
    connection — an adapter that verified only the token would store it and
    fail later, which is exactly the drift D-OH17.8 exists to prevent."""
    bad = GustoAdapter(base_url="http://mock-gusto", api_token="mock",
                       company_id="somebody-elses-company",
                       transport=SyncASGITransport(create_mock_gusto()))
    with pytest.raises(ProviderError) as denied:
        bad.verify()
    assert "404" in str(denied.value)


def test_verify_raises_on_a_bad_adp_secret() -> None:
    """ADP's client-credentials grant IS its verification: a wrong secret
    cannot mint a bearer, so the grant refuses."""
    bad = AdpAdapter(base_url="http://mock-adp", client_id="mock", client_secret="wrong",
                     transport=SyncASGITransport(create_mock_adp()))
    with pytest.raises(ProviderError) as denied:
        bad.verify()
    assert "401" in str(denied.value)


def test_verify_touches_only_read_only_wire_calls() -> None:
    """Read-only BY CONTRACT, checked at the wire. Do not "improve" verify()
    into a create-and-delete probe or a zero-dollar test payroll: it runs
    against a tenant's real payroll account, and a write there is not
    recoverable by deleting it afterwards."""
    gusto_wire = _RecordingTransport(SyncASGITransport(create_mock_gusto()))
    GustoAdapter(base_url="http://mock-gusto", api_token="mock", company_id="mock",
                 transport=gusto_wire).verify()
    assert gusto_wire.calls == [("GET", "/v1/companies/mock")]

    adp_wire = _RecordingTransport(SyncASGITransport(create_mock_adp()))
    AdpAdapter(base_url="http://mock-adp", client_id="mock", client_secret="mock",
               transport=adp_wire).verify()
    # ADP's single call is write-SHAPED (the OAuth grant is a POST) but
    # creates nothing in the tenant's payroll account — it mints a bearer.
    # No /hr/ or /payroll/ resource is touched.
    assert adp_wire.calls == [("POST", "/auth/oauth/v2/token")]


class _CountingTransport(httpx.BaseTransport):
    """Wraps the ADP mock and counts token-grant requests."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.token_grants = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/oauth/v2/token"):
            self.token_grants += 1
        return self._inner.handle_request(request)


def test_adp_verify_authenticates_even_on_an_adapter_that_already_has_a_bearer():
    """`AdpAdapter.verify()` clears the cached bearer before minting one, and
    NOTHING pinned that until 2026-08-31.

    Every other ADP test builds a fresh adapter, which has no cached token, so
    `verify()` reaches the wire whether or not the clear is there. Deleting
    `self._access_token = None` — restoring the exact silent no-op the method's
    own docstring says it exists to prevent — left all eleven ADP tests green.
    Found by mutation testing in review.

    So this test does the one thing the others cannot: it REUSES an adapter
    that has already authenticated. The other three adapters need no
    equivalent — they call the wire unconditionally, with no cache to skip."""
    counting = _CountingTransport(SyncASGITransport(create_mock_adp()))
    adapter = AdpAdapter(base_url="http://mock-adp", client_id="mock",
                         client_secret="mock", transport=counting)

    adapter.verify()
    assert counting.token_grants == 1
    first = adapter._access_token
    assert first is not None  # the adapter is now in the CACHED state

    adapter.verify()
    # The assertion that dies without the cache clear: a second grant, not a
    # cheap return off the cached bearer.
    assert counting.token_grants == 2
