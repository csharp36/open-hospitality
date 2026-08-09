"""Sealed-PII vault API (Pillar C1).

The vault has NO read path. Writes accept opaque HPKE envelopes (validated for
STRUCTURE only — never opened, which would put plaintext server-side outside
C2's provider-send path); reads return "on file / not on file" booleans only.
All routes past the public key are Payroll-Admin-gated and audited.
"""

import base64
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from usali.auth import PAYROLL_ADMIN, Principal, request_session_factory, require_grants
from usali.config import get_settings
from usali.deposit_accounts import AllocationSpec, allocation_violation
from usali.models import (
    AuditEvent,
    DepositAccount,
    Employee,
    EmployeePayrollProfile,
    PaySchedule,
    Property,
)
from usali.opener import Opener
from usali.pii_crypto import EnvelopeError, SealedEnvelope

router = APIRouter(prefix="/api/payroll")

require_payroll_admin = require_grants(PAYROLL_ADMIN)

# The sealed columns a blind-overwrite write may set. Each maps to `<field>_sealed`.
# bank_account / bank_routing left in E5 — the deposit destination is a CHAIN
# on the deposit-accounts surface below, and the old write path is DEAD in the
# same release (no dual write paths through money routing).
_SEALED_FIELDS = ("ssn", "tax_elections")


def _opener(request: Request) -> Opener:
    op: Opener = request.app.state.opener
    return op


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


class PublicKeyModel(BaseModel):
    key_id: str
    suite: str
    public_key: str  # base64 SEC1 uncompressed point


@router.get("/pii-public-key")
def pii_public_key(request: Request) -> PublicKeyModel:
    """The recipient public key the client seals PII against. Served on the
    authenticated channel so the client can pin it (a substituted key would
    silently defeat the sealing)."""
    key = _opener(request).public_key()
    return PublicKeyModel(
        key_id=key.key_id, suite=key.suite,
        public_key=base64.b64encode(key.public_key).decode("ascii"),
    )


class ProfileBody(BaseModel):
    ssn: str | None = None
    tax_elections: str | None = None


class ProfileStatus(BaseModel):
    employee_id: int
    ssn_on_file: bool
    tax_elections_on_file: bool


def _status(profile: EmployeePayrollProfile) -> ProfileStatus:
    return ProfileStatus(
        employee_id=profile.employee_id,
        ssn_on_file=profile.ssn_sealed is not None,
        tax_elections_on_file=profile.tax_elections_sealed is not None,
    )


def _validate_sealed(value: str, current_key_id: str) -> str:
    """Validate envelope STRUCTURE only (never open it). A caller that seals to a
    foreign/attacker key_id — or ships a wrong-suite/wrong-length envelope — is
    rejected here, before any DB write."""
    try:
        env = SealedEnvelope.parse(value)
    except EnvelopeError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid sealed envelope: {exc}"
        ) from exc
    if env.key_id != current_key_id:
        raise HTTPException(
            status_code=422,
            detail=f"envelope key_id {env.key_id!r} is not the current key",
        )
    return value


@router.put("/employees/{employee_id}/profile")
def put_profile(
    employee_id: int,
    body: ProfileBody,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> ProfileStatus:
    """Blind-overwrite: store sealed envelopes verbatim. Provided fields are set;
    omitted fields are left untouched. No plaintext, no open — structure is
    validated only, and EVERY provided field is validated before any write
    (all-or-nothing)."""
    current_key_id = _opener(request).public_key().key_id
    # Validate every provided sealed field BEFORE touching the DB (all-or-nothing:
    # one malformed field aborts the whole write with 422, storing nothing).
    validated = {
        field: _validate_sealed(getattr(body, field), current_key_id)
        for field in _SEALED_FIELDS
        if getattr(body, field) is not None
    }
    with _session(request) as session:
        employee = session.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="employee not found")
        profile = session.execute(
            select(EmployeePayrollProfile).where(
                EmployeePayrollProfile.employee_id == employee_id
            )
        ).scalar_one_or_none()
        if profile is None:
            profile = EmployeePayrollProfile(employee_id=employee_id)
            session.add(profile)
        for field, sealed in validated.items():
            setattr(profile, f"{field}_sealed", sealed)
        session.add(
            AuditEvent(
                actor_subject=principal.subject,
                action="write_payroll_pii",
                resource_type="employee",
                resource_id=str(employee_id),
            )
        )
        session.flush()
        status = _status(profile)
        session.commit()
        return status


@router.get("/employees/{employee_id}/profile")
def get_profile(
    employee_id: int,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> ProfileStatus:
    """On file / not on file ONLY — never a sealed value or plaintext (the vault
    is blind overwrite: correcting a value is a full re-entry + re-seal).

    Vault access is audited even though this returns booleans only: the
    AuditEvent contract records every PII read. The row carries resource_id
    (the employee) ONLY — never a value."""
    with _session(request) as session:
        if session.get(Employee, employee_id) is None:
            raise HTTPException(status_code=404, detail="employee not found")
        profile = session.execute(
            select(EmployeePayrollProfile).where(
                EmployeePayrollProfile.employee_id == employee_id
            )
        ).scalar_one_or_none()
        session.add(
            AuditEvent(
                actor_subject=principal.subject,
                action="read_payroll_pii_status",
                resource_type="employee",
                resource_id=str(employee_id),
            )
        )
        if profile is None:
            status = ProfileStatus(
                employee_id=employee_id, ssn_on_file=False,
                tax_elections_on_file=False,
            )
        else:
            status = _status(profile)
        session.commit()
        return status


class DepositAccountBody(BaseModel):
    allocation_type: str
    allocation_value: Decimal | None = None
    # Constrained at the boundary so a bad value is a 422, never a DB 500.
    # (NULL account_type exists only on backfilled rows; every NEW write
    # states a real type.)
    account_type: Literal["checking", "savings"]
    sealed_account: str
    sealed_routing: str

    @field_validator("allocation_value")
    @classmethod
    def _fits_the_column(cls, v: Decimal | None) -> Decimal | None:
        """The column is Numeric(10, 2). Without this bound, Postgres SILENTLY
        ROUNDS on insert -- the E5 review reproduced 99.999 accepted as legal,
        stored as 100.00, and then refused by preflight: the one shared
        allocation predicate judged a value the database never kept, and the
        API's 200 showed the caller a phantom. More than 2 decimal places is a
        422 HERE, so the value judged IS the value stored; the magnitude bound
        turns the numeric-overflow 500 into a 422 as well."""
        if v is None:
            return v
        if v != v.quantize(Decimal("0.01")):
            raise ValueError(
                "allocation_value must have at most two decimal places"
            )
        if abs(v) >= Decimal("100000000"):
            raise ValueError("allocation_value is out of range")
        return v


class DepositAccountsBody(BaseModel):
    accounts: list[DepositAccountBody]
    # An empty chain ERASES every deposit account (the next preflight blocks
    # the employee by name). A buggy client one retry away from an empty
    # array must not be able to de-bank someone with a 200 -- clearing is an
    # explicit act.
    clear: bool = False


class DepositAccountStatus(BaseModel):
    ordinal: int
    allocation_type: str
    allocation_value: str | None
    # None only on backfilled rows whose pre-E5 profile never stated a type.
    account_type: str | None
    account_on_file: bool
    routing_on_file: bool


class DepositAccountsStatus(BaseModel):
    employee_id: int
    accounts: list[DepositAccountStatus]


def _deposit_status(employee_id: int, rows: list[DepositAccount]) -> DepositAccountsStatus:
    return DepositAccountsStatus(
        employee_id=employee_id,
        accounts=[
            DepositAccountStatus(
                ordinal=r.ordinal,
                allocation_type=r.allocation_type,
                allocation_value=(
                    # Always 2-dp ("50.00", never "50") so a PUT response and
                    # a later GET render the SAME string — the boundary
                    # validator guarantees the stored value IS 2-dp.
                    str(r.allocation_value.quantize(Decimal("0.01")))
                    if r.allocation_value is not None else None
                ),
                account_type=r.account_type,
                # A backfilled half-entered profile holds '' in the never-
                # sealed slot; it is not on file, exactly as the missing
                # COLUMN was not before E5.
                account_on_file=r.sealed_account != "",
                routing_on_file=r.sealed_routing != "",
            )
            for r in sorted(rows, key=lambda r: r.ordinal)
        ],
    )


@router.put("/employees/{employee_id}/deposit-accounts")
def put_deposit_accounts(
    employee_id: int,
    body: DepositAccountsBody,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> DepositAccountsStatus:
    """FULL-REPLACE of the employee's whole chain, atomically. C1's contract
    is already "correcting a value is a full re-entry + re-seal" — per-row
    patching would invite half-updated chains where account 2 is new and
    account 3 still points at a closed account. Ordinals are assigned here
    from array position (1..N), so gaps and duplicates are unrepresentable.

    Everything is validated BEFORE any write (all-or-nothing): every envelope
    for structure and current key — never opened — and the allocation shape
    through `allocation_violation`, the same function preflight uses, so the
    two doors cannot drift on what a legal chain is.
    """
    if not body.accounts and not body.clear:
        raise HTTPException(
            status_code=422,
            detail="an empty accounts list erases the whole chain; send "
                   "clear=true to confirm",
        )
    violation = allocation_violation([
        AllocationSpec(allocation_type=a.allocation_type,
                       allocation_value=a.allocation_value)
        for a in body.accounts
    ])
    if violation is not None:
        raise HTTPException(status_code=422, detail=violation)
    current_key_id = _opener(request).public_key().key_id
    for account in body.accounts:
        _validate_sealed(account.sealed_account, current_key_id)
        _validate_sealed(account.sealed_routing, current_key_id)
    with _session(request) as session:
        if session.get(Employee, employee_id) is None:
            raise HTTPException(status_code=404, detail="employee not found")
        session.execute(
            delete(DepositAccount).where(
                DepositAccount.employee_id == employee_id
            )
        )
        rows = [
            DepositAccount(
                employee_id=employee_id,
                ordinal=i,
                allocation_type=a.allocation_type,
                allocation_value=a.allocation_value,
                account_type=a.account_type,
                sealed_account=a.sealed_account,
                sealed_routing=a.sealed_routing,
                legacy_sealed=False,
            )
            for i, a in enumerate(body.accounts, start=1)
        ]
        session.add_all(rows)
        session.add(
            AuditEvent(
                actor_subject=principal.subject,
                action="write_deposit_accounts",
                resource_type="employee",
                resource_id=str(employee_id),
            )
        )
        session.flush()
        status = _deposit_status(employee_id, rows)
        session.commit()
        return status


@router.get("/employees/{employee_id}/deposit-accounts")
def get_deposit_accounts(
    employee_id: int,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> DepositAccountsStatus:
    """Booleans + allocation metadata ONLY — never a sealed value (the vault
    has no read path). Audited like every vault access, even though it
    returns no secret: the AuditEvent contract records every PII-adjacent
    read, resource id only."""
    with _session(request) as session:
        if session.get(Employee, employee_id) is None:
            raise HTTPException(status_code=404, detail="employee not found")
        rows = list(session.execute(
            select(DepositAccount).where(
                DepositAccount.employee_id == employee_id
            )
        ).scalars())
        session.add(
            AuditEvent(
                actor_subject=principal.subject,
                action="read_deposit_accounts_status",
                resource_type="employee",
                resource_id=str(employee_id),
            )
        )
        status = _deposit_status(employee_id, rows)
        session.commit()
        return status


class PayScheduleBody(BaseModel):
    """A property's pay schedule. `anchor` is display/config metadata
    constrained to the platform payroll grid: all period math runs on
    settings.payroll_period_anchor, so the anchor must sit on that biweekly
    grid (per-property grids are future work)."""

    frequency: str = "biweekly"
    anchor: date
    check_date_offset_days: int = 5


class PayScheduleModel(BaseModel):
    property_id: str
    frequency: str
    anchor: date
    check_date_offset_days: int


def _pay_schedule_model(schedule: PaySchedule) -> PayScheduleModel:
    return PayScheduleModel(
        property_id=schedule.property_id,
        frequency=schedule.frequency,
        anchor=schedule.anchor,
        check_date_offset_days=schedule.check_date_offset_days,
    )


@router.put("/properties/{property_id}/pay-schedule")
def put_pay_schedule(
    property_id: str,
    body: PayScheduleBody,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> PayScheduleModel:
    """Payroll-Admin-only: set (get-or-create) a property's pay schedule, audited.

    The anchor must align with the platform payroll grid (a whole number of
    14-day periods from settings.payroll_period_anchor) — a misaligned anchor
    would be dead config on a money path, silently diverging from every period
    computation and producing confusing "no timecards" preflights."""
    platform_anchor = get_settings().payroll_period_anchor
    if (body.anchor - platform_anchor).days % 14 != 0:
        raise HTTPException(
            status_code=422,
            detail=(
                f"anchor {body.anchor.isoformat()} does not align with the "
                f"platform payroll grid (anchored at "
                f"{platform_anchor.isoformat()}, biweekly); per-property "
                "grids are future work"
            ),
        )
    with _session(request) as session:
        if session.get(Property, property_id) is None:
            raise HTTPException(status_code=404, detail="property not found")
        schedule = session.execute(
            select(PaySchedule).where(PaySchedule.property_id == property_id)
        ).scalar_one_or_none()
        if schedule is None:
            schedule = PaySchedule(property_id=property_id, anchor=body.anchor)
            session.add(schedule)
        schedule.frequency = body.frequency
        schedule.anchor = body.anchor
        schedule.check_date_offset_days = body.check_date_offset_days
        session.add(
            AuditEvent(
                actor_subject=principal.subject,
                action="write_pay_schedule",
                resource_type="property",
                resource_id=property_id,
            )
        )
        session.flush()
        model = _pay_schedule_model(schedule)
        session.commit()
        return model


@router.get("/properties/{property_id}/pay-schedule")
def get_pay_schedule(
    property_id: str,
    request: Request,
    principal: Principal = Depends(require_payroll_admin),
) -> PayScheduleModel:
    """Payroll-Admin-only: read a property's pay schedule."""
    with _session(request) as session:
        schedule = session.execute(
            select(PaySchedule).where(PaySchedule.property_id == property_id)
        ).scalar_one_or_none()
        if schedule is None:
            raise HTTPException(status_code=404, detail="pay schedule not found")
        return _pay_schedule_model(schedule)
