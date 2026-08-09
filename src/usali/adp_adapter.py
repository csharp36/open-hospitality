"""ADP-shaped payroll adapter (Pillar C2). Real httpx; the mock or the real
endpoint is a base_url + credentials change. Maps canonical port types to
ADP's camelCase-envelope / integer-cents wire shape, and speaks the OAuth
client-credentials grant (cached bearer; one refresh-and-retry on a 401).

SECURITY: error messages carry status + the provider's response detail only —
NEVER the request payload (it contains SSN/bank at sync time). For the sync
path even the RESPONSE detail is dropped (include_detail=False): a real
provider's validation error can echo the offending SSN/bank value back, so a
sync error message is status code + fixed text only.
"""

import base64
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from usali.config import Settings
from usali.payroll_provider import (
    PayrollEmployee,
    PayRunEntry,
    PayRunResult,
    PayRunResultLine,
    ProviderCapabilities,
    ProviderError,
    ProviderRun,
)

_CENTS = Decimal("0.01")
_TOKEN_PATH = "/auth/oauth/v2/token"
_STATUS = {"SUBMITTED": "submitted", "PROCESSED": "processed"}


def _dollars(cents: int) -> Decimal:
    """Integer wire cents → canonical Decimal dollars, quantized to 0.01."""
    return (Decimal(cents) / 100).quantize(_CENTS)


class AdpAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
        self._basic_auth = f"Basic {basic}"
        self._access_token: str | None = None
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=15,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "AdpAdapter":
        return cls(base_url=settings.adp_base_url, client_id=settings.adp_client_id,
                   client_secret=settings.adp_client_secret)

    def capabilities(self) -> ProviderCapabilities:
        # sick_balance_display stays False until the sandbox verifies ADP
        # renders an externally-tracked balance (G5/go-live) — callers gate
        # on it, so False means "named blocker", never a silent no-op.
        return ProviderCapabilities(
            supports_field_encryption=False,
            supports_employee_update=True,
        )

    def update_employee(
        self, provider_employee_id: str, employee: PayrollEmployee
    ) -> None:
        # Full-replace (G plan decision 1): the SAME payload shape as first
        # sync, PUT to the worker's own resource. Sync-path error posture:
        # the request carries plaintext SSN/bank, so no response detail.
        self._request(
            "PUT", f"/hr/v1/workers/{provider_employee_id}",
            doing="worker update", include_detail=False,
            json=self._worker_payload(employee),
        )

    def _raise_for(
        self, resp: httpx.Response, doing: str, *, include_detail: bool = True
    ) -> None:
        if resp.status_code < 400:
            return
        if not include_detail:
            # Sync-time errors: the REQUEST carried plaintext SSN/bank, and a
            # badly-behaved provider may echo it in the response. Fixed message.
            raise ProviderError(f"adp {doing} failed ({resp.status_code})")
        detail = ""
        try:
            detail = str(resp.json().get("detail", ""))[:200]
        except Exception:  # noqa: BLE001 - non-JSON error body
            detail = resp.text[:200]
        raise ProviderError(f"adp {doing} failed ({resp.status_code}): {detail}")

    def _token(self) -> str:
        """Lazily mint (and cache) a bearer via the client-credentials grant."""
        if self._access_token is None:
            try:
                resp = self._http.post(
                    _TOKEN_PATH,
                    data={"grant_type": "client_credentials"},
                    headers={"Authorization": self._basic_auth},
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"adp unreachable: {type(exc).__name__}") from exc
            self._raise_for(resp, "token grant")
            self._access_token = str(resp.json()["access_token"])
        return self._access_token

    def _request(
        self, method: str, path: str, *, doing: str,
        json: dict[str, Any] | None = None, include_detail: bool = True,
    ) -> httpx.Response:
        """One business call: cached bearer; on a 401 clear the cache and
        retry ONCE (mirrors QboClient's refreshed_after_401 guard)."""
        refreshed_after_401 = False
        while True:
            try:
                resp = self._http.request(
                    method, path, json=json,
                    headers={"Authorization": f"Bearer {self._token()}"},
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"adp unreachable: {type(exc).__name__}") from exc
            if resp.status_code == 401 and not refreshed_after_401:
                # Token expired/revoked mid-flight: re-grant once and retry.
                refreshed_after_401 = True
                self._access_token = None
                continue
            self._raise_for(resp, doing, include_detail=include_detail)
            return resp

    @staticmethod
    def _worker_payload(employee: PayrollEmployee) -> dict[str, object]:
        """ONE serialization for create and update (G4): two copies would be
        two chances for the wire shapes to drift apart."""
        return {
            "worker": {
                "legalName": employee.full_name,
                "governmentID": employee.ssn,
                # E5: `depositAccount` (singular) became `depositAccounts`
                # in ordinal order, each carrying its allocation.
                "depositAccounts": [
                    {
                        "routingNumber": a.routing,
                        "accountNumber": a.account,
                        "accountType": a.account_type,
                        "allocationType": a.allocation_type,
                        "allocationValue": (
                            str(a.allocation_value)
                            if a.allocation_value is not None else None
                        ),
                    }
                    for a in employee.deposit_accounts
                ],
            },
        }

    def sync_employee(self, employee: PayrollEmployee) -> str:
        resp = self._request(
            "POST", "/hr/v1/workers", doing="worker sync", include_detail=False,
            json=self._worker_payload(employee),
        )
        return str(resp.json()["worker"]["associateOID"])

    def submit_pay_run(
        self,
        *,
        period_start: date,
        period_end: date,
        check_date: date,
        entries: list[PayRunEntry],
    ) -> ProviderRun:
        # G5 produces the balance display figure upstream, but this
        # adapter declares the capability False until the sandbox verifies
        # ADP renders an external balance — an entry carrying it must
        # refuse rather than silently drop the §246(i) figure the caller
        # meant to show.
        if any(e.sick_balance_hours is not None for e in entries):
            raise ProviderError(
                "adp adapter cannot carry a sick balance yet; refusing "
                "rather than silently dropping the wage-statement figure"
            )
        payload = {
            "payrollRun": {
                "periodStartDate": period_start.isoformat(),
                "periodEndDate": period_end.isoformat(),
                "checkDate": check_date.isoformat(),
                "earnings": [
                    {
                        "associateOID": e.provider_employee_id,
                        "regularHours": float(str(e.regular_hours)),
                        "overtimeHours": float(str(e.ot_hours)),
                        "doubleTimeHours": float(str(e.dt_hours)),
                        "hourlyRateInCents": int(e.hourly_rate * 100),
                        # G4: sick hours are their OWN bucket at straight
                        # time — folding them into regularHours would lie
                        # to the provider's OT math and the pay stub alike.
                        "sickHours": float(str(e.sick_hours)),
                    }
                    for e in entries
                ],
            },
        }
        resp = self._request("POST", "/payroll/v1/payroll-runs",
                             doing="payroll submit", json=payload)
        run = resp.json()["payrollRun"]
        return ProviderRun(
            provider_run_id=str(run["payrollRunID"]),
            status=_STATUS.get(run["processingStatus"], str(run["processingStatus"]).lower()),
        )

    def get_pay_run(self, provider_run_id: str) -> PayRunResult:
        resp = self._request("GET", f"/payroll/v1/payroll-runs/{provider_run_id}",
                             doing="payroll fetch")
        run = resp.json()["payrollRun"]
        return PayRunResult(
            status=_STATUS.get(run["processingStatus"], str(run["processingStatus"]).lower()),
            lines=[
                PayRunResultLine(
                    provider_employee_id=p["associateOID"],
                    gross=_dollars(p["grossPayInCents"]),
                    employee_taxes=_dollars(p["employeeTaxesInCents"]),
                    employer_taxes=_dollars(p["employerTaxesInCents"]),
                    net=_dollars(p["netPayInCents"]),
                )
                for p in run["workerPayments"]
            ],
        )
