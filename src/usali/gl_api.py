"""The /api/gl surface (OH-27 design §5): chart of accounts, fiscal
periods, the trial balance, and posting.

Reads ride the router-level operator gates `create_app` mounts this
router with; mutations (chart edits, close/reopen, post) narrow to
org_admin through `require_gl_admin` — the `integrations_api` precedent:
editing the tenant's chart or closing its books is a standing commitment
about the TENANT, not any one property's operator concern.

Amounts serialize as `str(Decimal)` — the `journal_entry_body`
precedent: exact through JSON, no float round-trip.
"""

from datetime import date, timedelta
from typing import Callable, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali import fiscal, gl_posting, reporting
from usali.auth import ORG_ADMIN, Principal, request_session_factory, require_grants
from usali.models import GlAccount
from usali.tenancy import current_org_id

router = APIRouter(prefix="/api/gl")

require_gl_admin = require_grants(ORG_ADMIN)

T = TypeVar("T")

# The six legal account types — ck_gl_account_type is the DB wall for the
# same set; checking here turns its IntegrityError 500 into a legible 422.
_ACCOUNT_TYPES = frozenset(
    {"asset", "contra_asset", "liability", "equity", "income", "expense"}
)


def _session(request: Request) -> Session:
    return request_session_factory(request)()


def _run(query: Callable[[], T]) -> T:
    """Run one GL query, mapping its typed refusals to HTTP errors —
    `portal_api._run`'s shape, plus the fiscal-config arm this module
    needs. `NoFactsError` ("nothing there") -> 404;
    `FiscalCalendarNotConfigured` (not a ValueError, so its own arm,
    message passed through whole) -> 422; any other ValueError (a bad
    request the query rejected, e.g. an empty reopen reason or a
    malformed period key) -> 422.
    """
    try:
        return query()
    except reporting.NoFactsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except fiscal.FiscalCalendarNotConfigured as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- Response/request models -------------------------------------------------


class AccountModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_code: str
    name: str
    account_type: str
    system_role: str | None
    usali_schedule_id: int | None
    is_active: bool


class AccountPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    account_type: str
    is_active: bool = True


class PeriodModel(BaseModel):
    """One fiscal period with its derived state and both gap directions
    (`gl_posting.CloseGaps`): `unposted_dates` — financial-fact dates with
    no current posted `pms_daily` entry (pms_daily-only by design: "facts
    with no entry" means `usali_financial_fact` here; a labor day with no
    accrual is the ordinary no-chart/no-cost case, not a gap);
    `orphaned_dates` — dates with a current entry whose fact side is
    empty, covering BOTH sources (`usali_financial_fact` for `pms_daily`,
    `usali_labor_fact` for `payroll_accrual`)."""

    model_config = ConfigDict(extra="forbid")

    period_key: str
    state: str
    unposted_dates: list[date]
    orphaned_dates: list[date]


class CloseResponse(BaseModel):
    """The close's answer: the period's state after the event, and the
    gaps as they stand — same two directions and the same scoping as
    `PeriodModel` (see its docstring / `gl_posting.CloseGaps`), so
    closing over either kind of gap is a visible choice."""

    model_config = ConfigDict(extra="forbid")

    period_key: str
    state: str
    unposted_dates: list[date]
    orphaned_dates: list[date]


class ReopenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str


class PostBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_id: str
    date_from: date
    date_to: date


class PostOutcomeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business_date: date
    source_type: str
    # Annotated with the engine's own Literal, so the admitted set can
    # never drift from `gl_posting.OutcomeStatus` — the authority.
    status: gl_posting.OutcomeStatus
    message: str | None


class TrialBalanceLineModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_code: str
    name: str
    account_type: str
    debits: str
    credits: str


class TrialBalanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    property_id: str
    period_key: str
    date_from: date
    date_to: date
    lines: list[TrialBalanceLineModel]
    total_debits: str
    total_credits: str


# --- The chart ---------------------------------------------------------------


@router.get("/accounts")
def get_accounts(request: Request) -> list[AccountModel]:
    """The org's whole chart, inactive rows included — the management
    surface needs to show what a deactivation hid. Org scoping is the
    session's (GlAccount is OrgScoped; both L2 walls confine the read)."""
    with _session(request) as session:
        rows = session.scalars(
            select(GlAccount).order_by(GlAccount.account_code)
        ).all()
        return [
            AccountModel(
                account_code=row.account_code,
                name=row.name,
                account_type=row.account_type,
                system_role=row.system_role,
                usali_schedule_id=row.usali_schedule_id,
                is_active=row.is_active,
            )
            for row in rows
        ]


@router.put("/accounts/{account_code}", status_code=204)
def put_account(
    account_code: str,
    body: AccountPut,
    request: Request,
    principal: Principal = Depends(require_gl_admin),
) -> Response:
    """Upsert one chart row: a full replace of the editable fields, and
    the row is created when absent (an org extension beyond the seeded
    template). Deactivating a role-bearing account is refused HERE — the
    D-OH27.2 enforcement point GlAccount's docstring names: the posting
    engine resolves structural accounts by `system_role`, so hiding the
    role's carrier would break the next post, not this request."""
    del principal  # the gate is the point; GlAccount carries no actor column
    if body.account_type not in _ACCOUNT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"account_type must be one of {sorted(_ACCOUNT_TYPES)}",
        )
    with _session(request) as session:
        row = session.scalar(
            select(GlAccount).where(GlAccount.account_code == account_code)
        )
        if row is not None and row.system_role is not None and not body.is_active:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"account {account_code} carries system role "
                    f"{row.system_role!r}; the posting engine resolves that "
                    "account by role, so it cannot be deactivated — move the "
                    "role to another account first"
                ),
            )
        if row is None:
            session.add(
                GlAccount(
                    org_id=current_org_id(session),
                    account_code=account_code,
                    name=body.name,
                    account_type=body.account_type,
                    is_active=body.is_active,
                )
            )
        else:
            row.name = body.name
            row.account_type = body.account_type
            row.is_active = body.is_active
        session.commit()
    return Response(status_code=204)


# --- Periods -----------------------------------------------------------------


@router.get("/periods")
def get_periods(
    request: Request,
    property_id: str = Query(alias="property"),
    fiscal_year: int | None = Query(default=None),
) -> list[PeriodModel]:
    """Every period of one fiscal year (default: the year containing
    today), each with its derived state and both gap directions —
    `period_state` and `period_gaps`, the same derivations close uses,
    so this listing and the close can never name different gaps."""
    with _session(request) as session:

        def _list() -> list[PeriodModel]:
            cfg = fiscal.require_config(fiscal.config_for(session, property_id))
            year = fiscal_year
            if year is None:
                today_key = fiscal.period_containing(cfg, date.today())
                year = int(today_key.split("-P")[0])
            out: list[PeriodModel] = []
            for key, _start, _end in fiscal.periods_in_year(cfg, year):
                gaps = gl_posting.period_gaps(
                    session, property_id=property_id, period_key=key
                )
                out.append(
                    PeriodModel(
                        period_key=key,
                        state=gl_posting.period_state(session, property_id, key),
                        unposted_dates=gaps.unposted,
                        orphaned_dates=gaps.orphaned,
                    )
                )
            return out

        return _run(_list)


@router.put("/periods/{period_key}/close")
def close_period(
    period_key: str,
    request: Request,
    property_id: str = Query(alias="property"),
    principal: Principal = Depends(require_gl_admin),
) -> CloseResponse:
    """Close one period (idempotent — `gl_posting.close_period`), and
    answer with the gaps so closing over either kind is a visible choice."""
    with _session(request) as session:
        gaps = _run(
            lambda: gl_posting.close_period(
                session,
                property_id=property_id,
                period_key=period_key,
                actor=principal.subject,
            )
        )
        state = gl_posting.period_state(session, property_id, period_key)
        session.commit()
    return CloseResponse(
        period_key=period_key,
        state=state,
        unposted_dates=gaps.unposted,
        orphaned_dates=gaps.orphaned,
    )


@router.put("/periods/{period_key}/reopen", status_code=204)
def reopen_period(
    period_key: str,
    body: ReopenBody,
    request: Request,
    property_id: str = Query(alias="property"),
    principal: Principal = Depends(require_gl_admin),
) -> Response:
    """Reopen one period (idempotent). An empty reason is refused —
    `gl_posting.reopen_period`'s ValueError, mapped to 422 by `_run`."""
    with _session(request) as session:
        _run(
            lambda: gl_posting.reopen_period(
                session,
                property_id=property_id,
                period_key=period_key,
                actor=principal.subject,
                reason=body.reason,
            )
        )
        session.commit()
    return Response(status_code=204)


# --- Posting -----------------------------------------------------------------


@router.post("/post")
def post_range(
    body: PostBody,
    request: Request,
    principal: Principal = Depends(require_gl_admin),
) -> list[PostOutcomeModel]:
    """Post (or re-post) every source for each date in the range — the
    CLI `gl-post` loop behind HTTP: dates x `POSTING_SOURCES` through
    `post_and_record`, one commit at the end. No chart is not an error
    here: `post_and_record` decides that per grain and answers "skipped",
    so the caller sees honest per-grain statuses either way."""
    if body.date_from > body.date_to:
        raise HTTPException(
            status_code=422,
            detail=f"{body.date_from.isoformat()} is after {body.date_to.isoformat()}",
        )
    outcomes: list[PostOutcomeModel] = []
    with _session(request) as session:
        day = body.date_from
        while day <= body.date_to:
            for source in gl_posting.POSTING_SOURCES:
                out = gl_posting.post_and_record(
                    session,
                    property_id=body.property_id,
                    business_date=day,
                    source_type=source.source_type,
                    actor=principal.subject,
                )
                outcomes.append(
                    PostOutcomeModel(
                        business_date=day,
                        source_type=source.source_type,
                        status=out.status,
                        message=out.message,
                    )
                )
            day += timedelta(days=1)
        session.commit()
    return outcomes


# --- The trial balance -------------------------------------------------------


@router.get("/trial-balance")
def get_trial_balance(
    request: Request,
    property_id: str = Query(alias="property"),
    period: str = Query(),
) -> TrialBalanceModel:
    """`reporting.trial_balance`, field-for-field; Decimals as strings."""
    with _session(request) as session:
        report = _run(
            lambda: reporting.trial_balance(
                session, property_id=property_id, period_key=period
            )
        )
    return TrialBalanceModel(
        property_id=report.property_id,
        period_key=report.period_key,
        date_from=report.date_from,
        date_to=report.date_to,
        lines=[
            TrialBalanceLineModel(
                account_code=line.account_code,
                name=line.name,
                account_type=line.account_type,
                debits=str(line.debits),
                credits=str(line.credits),
            )
            for line in report.lines
        ],
        total_debits=str(report.total_debits),
        total_credits=str(report.total_credits),
    )
