"""Gusto-shaped payroll adapter (Pillar C2). Real httpx; the mock or the real
endpoint is a base_url + token change. Maps canonical port types to Gusto's
snake_case / decimal-string-dollars wire shape.

SECURITY: error messages carry status + the provider's response detail only —
NEVER the request payload (it contains SSN/bank at sync time). For the sync
path even the RESPONSE detail is dropped (include_detail=False): a real
provider's validation error can echo the offending SSN/bank value back, so a
sync error message is status code + fixed text only.
"""

from datetime import date
from decimal import Decimal

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


class GustoAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        company_id: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._company_id = company_id
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=15,
            headers={"Authorization": f"Bearer {api_token}"},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "GustoAdapter":
        return cls(base_url=settings.gusto_base_url, api_token=settings.gusto_api_token,
                   company_id=settings.gusto_company_id)

    def capabilities(self) -> ProviderCapabilities:
        # sick_balance_display stays False until the sandbox verifies Gusto
        # renders an externally-tracked balance (G5/go-live) — callers gate
        # on it, so False means "named blocker", never a silent no-op.
        return ProviderCapabilities(
            supports_field_encryption=False,
            supports_employee_update=True,
        )

    def verify(self) -> None:
        """D-OH17.8: read the company the token is scoped to. Proves BOTH
        halves of the credential — a valid token aimed at a company id this
        integration cannot reach is still a broken connection, and it fails
        HERE, at connect time, rather than on the tenant's first pay run.

        A plain GET: nothing is created, updated or submitted. `include_detail`
        is True here and only here on this adapter's error paths — unlike the
        sync path, this REQUEST carries no PII, so there is no SSN or account
        number a provider's validation error could echo back into the message,
        and the provider's own wording is genuinely useful to whoever just
        pasted the key."""
        try:
            resp = self._http.get(f"/v1/companies/{self._company_id}")
        except httpx.HTTPError as exc:
            raise ProviderError(f"gusto unreachable: {type(exc).__name__}") from exc
        self._raise_for(resp, "credential verify", include_detail=True)

    def update_employee(
        self, provider_employee_id: str, employee: PayrollEmployee
    ) -> None:
        # Full-replace (G plan decision 1): the SAME payload shape as first
        # sync, PUT to the employee's own resource. Sync-path error posture:
        # the request carries plaintext SSN/bank, so no response detail.
        try:
            resp = self._http.put(
                f"/v1/companies/{self._company_id}/employees/"
                f"{provider_employee_id}",
                json=self._employee_payload(employee),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"gusto unreachable: {type(exc).__name__}") from exc
        self._raise_for(resp, "employee update", include_detail=False)

    def _raise_for(
        self, resp: httpx.Response, doing: str, *, include_detail: bool = True
    ) -> None:
        if resp.status_code < 400:
            return
        if not include_detail:
            # Sync-time errors: the REQUEST carried plaintext SSN/bank, and a
            # badly-behaved provider may echo it in the response. Fixed message.
            raise ProviderError(f"gusto {doing} failed ({resp.status_code})")
        detail = ""
        try:
            detail = str(resp.json().get("detail", ""))[:200]
        except Exception:  # noqa: BLE001 - non-JSON error body
            detail = resp.text[:200]
        raise ProviderError(f"gusto {doing} failed ({resp.status_code}): {detail}")

    @staticmethod
    def _employee_payload(employee: PayrollEmployee) -> dict[str, object]:
        """ONE serialization for create and update (G4): two copies would be
        two chances for the wire shapes to drift apart."""
        return {
            "full_name": employee.full_name, "ssn": employee.ssn,
            # E5: the deposit destination is a CHAIN, in ordinal
            # order. `remainder` carries a null value — Gusto's
            # "everything left" split; amounts/percents carve first.
            "deposit_accounts": [
                {
                    "bank_routing": a.routing,
                    "bank_account": a.account,
                    "account_type": a.account_type,
                    "allocation_type": a.allocation_type,
                    "allocation_value": (
                        str(a.allocation_value)
                        if a.allocation_value is not None else None
                    ),
                }
                for a in employee.deposit_accounts
            ],
        }

    def sync_employee(self, employee: PayrollEmployee) -> str:
        try:
            resp = self._http.post(
                f"/v1/companies/{self._company_id}/employees",
                json=self._employee_payload(employee),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"gusto unreachable: {type(exc).__name__}") from exc
        self._raise_for(resp, "employee sync", include_detail=False)
        return str(resp.json()["uuid"])

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
        # Gusto renders an external balance — an entry carrying it must
        # refuse rather than silently drop the §246(i) figure the caller
        # meant to show.
        if any(e.sick_balance_hours is not None for e in entries):
            raise ProviderError(
                "gusto adapter cannot carry a sick balance yet; refusing "
                "rather than silently dropping the wage-statement figure"
            )
        payload = {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "check_date": check_date.isoformat(),
            "entries": [
                {
                    "employee_uuid": e.provider_employee_id,
                    "regular_hours": str(e.regular_hours),
                    "overtime_hours": str(e.ot_hours),
                    "double_overtime_hours": str(e.dt_hours),
                    "hourly_rate": str(e.hourly_rate),
                    # G4: sick hours are their OWN bucket at straight time —
                    # folding them into regular_hours would lie to the
                    # provider's OT math and the pay stub alike.
                    "sick_hours": str(e.sick_hours),
                }
                for e in entries
            ],
        }
        try:
            resp = self._http.post(f"/v1/companies/{self._company_id}/payrolls", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"gusto unreachable: {type(exc).__name__}") from exc
        self._raise_for(resp, "payroll submit")
        body = resp.json()
        return ProviderRun(provider_run_id=str(body["payroll_uuid"]), status="submitted")

    def get_pay_run(self, provider_run_id: str) -> PayRunResult:
        try:
            resp = self._http.get(
                f"/v1/companies/{self._company_id}/payrolls/{provider_run_id}"
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"gusto unreachable: {type(exc).__name__}") from exc
        self._raise_for(resp, "payroll fetch")
        body = resp.json()
        return PayRunResult(
            status=body["status"],
            lines=[
                PayRunResultLine(
                    provider_employee_id=c["employee_uuid"],
                    gross=Decimal(c["gross_pay"]),
                    employee_taxes=Decimal(c["employee_taxes"]),
                    employer_taxes=Decimal(c["employer_taxes"]),
                    net=Decimal(c["net_pay"]),
                )
                for c in body["employee_compensations"]
            ],
        )
