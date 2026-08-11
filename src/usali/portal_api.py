"""Portal API (P7 reads, P8 QBO push): the HTTP contract over `usali.reporting`
and `usali.qbo_push`.

This module owns everything HTTP about the portal: the `/api` router, Pydantic
response models mirroring the reporting/push dataclasses field-for-field, the
dataclass -> model conversion helpers, and the error mapping. `reporting.py`
and `qbo_push.py` stay pure Python (dataclasses in, dataclasses out) and know
nothing about FastAPI or serialization.

Conventions (shared with the P6 renderers — the frontend consumes both):

- Decimals serialize as exact strings, never floats. Every amount field on the
  models is a `str` built with `str(Decimal)`.
- Dates serialize as ISO strings (Pydantic's default for `datetime.date`).
- Reporting errors map to HTTP: `reporting.NoFactsError` (unknown property /
  empty window) -> 404; any other `ValueError` (inverted range, both date
  modes, multi-source property, missing mapping YAML) -> 422 with the message.
  Malformed query params (bad dates, missing required params) -> 422 via
  FastAPI's own validation.
- CPA-pack exception: `cpa_pack` signals an unknown property with a PLAIN
  ValueError (its "unknown property ..." message), not a `NoFactsError` — an
  empty month for a known property is a valid empty pack there, so the generic
  `_run` mapping would turn a typo'd property into a 422 "bad request". The
  endpoint maps that specific error to 404 explicitly (a missing resource),
  keying off the message prefix; every other ValueError (bad month, multi-
  source property) stays 422.
- QBO push errors: `UnmappedGlError` -> 422 with a STRUCTURED detail
  `{"unmapped": [{"major", "sub_category", "line_item"}, ...]}` — a curation
  worklist, not prose; transport failures (`httpx.HTTPError`) -> 502 with a
  clear "cannot reach QBO" detail instead of a raw 500 traceback.
- Endpoints are sync `def` on purpose: the QBO client rides on
  `SyncASGITransport` in tests, which needs a thread with no running event
  loop — FastAPI runs sync endpoints in a threadpool, satisfying that.

Coverage YAML paths: `coverage_report` defaults are CWD-relative, which breaks
`usali serve` when launched from outside the checkout. The API resolves
`mapping/segments.yaml` and `mapping/statistics.yaml` against the repo root
(two levels above this package — where `mapping/` lives), so the portal works
regardless of the working directory.
"""

from collections.abc import Callable, Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, TypeVar

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from usali import qbo_push, reporting
from usali.auth import Principal, request_session_factory, require_operator
from usali.fiscal import (
    FiscalCalendarNotConfigured,
    require_config,
    resolve_period,
)
from usali.inventory import InventoryNotConfigured
from usali.models import Property, QboPushLedger
from usali.performance import (
    AdrBasisUnavailable,
    Comparison,
    CoreMetrics,
    ReconLine,
    RollingStat,
    TrendPair,
    Trends,
    compare,
    complete_days,
    reconciliation,
    trends,
)
from usali.property_config_api import _adr_room_basis, _fiscal_config
from usali.qbo_client import QboClient
from usali.workforce import require_property_access, resolve_scope

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEGMENTS_YAML = _REPO_ROOT / "mapping" / "segments.yaml"
_STATISTICS_YAML = _REPO_ROOT / "mapping" / "statistics.yaml"

T = TypeVar("T")


def _get_session(request: Request) -> Iterator[Session]:
    """Per-request session from the factory `create_app` stashed on the app."""
    factory = request_session_factory(request)
    with factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(_get_session)]


def _get_qbo_client(request: Request) -> QboClient:
    """The app's ONE shared QBO client (built lazily by `create_app`'s factory).

    Refresh-token rotation lives in client memory (see the QboClient docstring),
    so a per-request client would invalid_grant on the second push. `create_app`
    wraps whatever factory it was given so this call always returns the same
    instance for the app's lifetime.
    """
    client: QboClient = request.app.state.get_qbo_client()
    return client


QboClientDep = Annotated[QboClient, Depends(_get_qbo_client)]


def _run(query: Callable[[], T]) -> T:
    """Run one reporting query, mapping its ValueErrors to HTTP errors.

    `NoFactsError` ("the property/window has no data") -> 404; any other
    ValueError (a bad request the query rejected) -> 422 with the message.
    """
    try:
        return query()
    except reporting.NoFactsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- Response models (field-for-field mirrors of the reporting dataclasses) ------


class SosLineModel(BaseModel):
    major: str
    sub_category: str
    line_item: str
    total: str


class DeptSectionModel(BaseModel):
    sub_category: str
    lines: list[SosLineModel]
    total: str


class SegmentLineModel(BaseModel):
    segment: str
    rooms: str
    room_revenue: str


class MetricRowModel(BaseModel):
    metric_code: str
    day: str | None
    mtd: str | None
    ytd: str | None
    day_prior: str | None
    mtd_prior: str | None
    ytd_prior: str | None


class LaborLineModel(BaseModel):
    department: str
    hours: str
    ot_hours: str
    # None when the department's cost is suppressed (single-employee rate-
    # derivation guard) — hours still carry.
    est_cost: str | None


class SickPayLineModel(BaseModel):
    department: str
    hours: str
    cost: str | None  # None = suppressed, same guard as est_cost


class LaborVarianceLineModel(BaseModel):
    department: str
    # None = suppressed (single-employee department, on EITHER side) — the
    # burden too, since burden ~ a fixed % of gross would re-derive the rate.
    est_cost: str | None
    actual_gross: str | None
    employer_burden: str | None
    variance: str | None
    hours_actual: str
    alert: bool


class LaborVarianceModel(BaseModel):
    lines: list[LaborVarianceLineModel]
    periods: list[str]
    est_total: str
    actual_total: str
    variance_total: str
    burden_total: str
    alert: bool
    suppressed_departments: int
    unpriced_hours: str


class SosReportModel(BaseModel):
    property_id: str
    pms_source: str
    business_date: date | None
    date_from: date | None
    date_to: date | None
    operated_departments: list[DeptSectionModel]
    misc_income: list[SosLineModel]
    misc_income_total: str
    total_operating_revenue: str
    taxes: list[SosLineModel]
    taxes_total: str
    settlements: list[SosLineModel]
    settlements_total: str
    other: list[SosLineModel]
    other_total: str
    rooms_segments: list[SegmentLineModel]
    statistics: list[MetricRowModel]
    # Pillar B3: labor unioned into the SOS. Schedule 14 = estimated payroll
    # expense; Schedule 15 = hours/OT/FTE. ESTIMATES, deliberately outside the
    # operating-revenue reconciliation.
    payroll_expense: list[LaborLineModel]
    payroll_expense_total: str
    labor_hours_total: str
    labor_ot_hours_total: str
    labor_fte: str | None
    # Count of departments whose cost is hidden (single-employee guard);
    # payroll_expense_total EXCLUDES them. Unpriced hours = hours worked at
    # est_cost 0 (no rate on file), so Schedule 14 can flag it honestly.
    labor_suppressed_departments: int
    labor_unpriced_hours: str
    # E4: sick pay taken in the window — the Schedule 14 benefits side. Cost
    # null when suppressed (one person's sick cost / hours IS their rate);
    # sick_pay_total EXCLUDES suppressed departments (complementary).
    sick_pay: list[SickPayLineModel]
    sick_pay_total: str
    sick_suppressed_departments: int
    sick_unpriced_hours: str
    # Pillar C3: estimate vs provider-actual for the pay periods intersecting
    # the window. Null when no processed pay run touches it.
    labor_variance: LaborVarianceModel | None


class NeedsReviewEntryModel(BaseModel):
    code: str
    line_item: str
    notes: str | None


class FinancialCoverageModel(BaseModel):
    dictionary_entries: int
    by_confidence: dict[str, int]
    by_review_status: dict[str, int]
    needs_review: list[NeedsReviewEntryModel]
    staged_codes: int
    mapped_codes: int
    missing_codes: list[str]
    exception_count: int
    gl_mapped: int
    gl_unmapped_codes: list[str]


class SegmentCoverageModel(BaseModel):
    dictionary_entries: int
    needs_review: list[NeedsReviewEntryModel]
    staged_codes: int
    mapped_codes: int
    unmapped_codes: list[str]


class StatisticsCoverageModel(BaseModel):
    dictionary_entries: int
    staged_labels: int
    mapped_labels: int
    unmapped_labels: list[str]


class SourceCoverageModel(BaseModel):
    pms_source: str
    financial: FinancialCoverageModel
    segments: SegmentCoverageModel
    statistics: StatisticsCoverageModel


class CoverageReportModel(BaseModel):
    sources: list[SourceCoverageModel]


class StagedTxnModel(BaseModel):
    stage_id: int
    pms_source: str
    business_date: date
    pms_trx_code: str
    pms_trx_desc: str | None
    amount: str
    source_file: str


class PropertyInfoModel(BaseModel):
    property_id: str
    pms_source: str
    first_date: date
    last_date: date
    # Display name from the property registry; None when the id has facts but
    # no registry row (possible in tests seeded without seed-properties).
    name: str | None = None


# --- CPA pack models (P8) ---------------------------------------------------------


class SalesLineModel(BaseModel):
    major: str
    sub_category: str
    line_item: str
    mtd_amount: str
    day_count: int


class SalesReportModel(BaseModel):
    lines: list[SalesLineModel]
    total_operating_revenue: str


class TaxLineModel(BaseModel):
    line_item: str
    gl_account_code: str | None
    mtd_amount: str


class TaxReportModel(BaseModel):
    lines: list[TaxLineModel]
    taxes_total: str
    room_revenue_base: str


class ArLineModel(BaseModel):
    """Mirror of `reporting.ArLine` — fields in chronological order.

    `opening_balance` is the FIRST reported balance IN the month, not the prior
    month's close — with daily trial-balance ingestion the two coincide, but
    after an ingestion gap the opening is simply the earliest date that
    reported. (Carried from the ArLine docstring; downstream mirrors of this
    shape must carry the same note.)
    """

    ledger_code: str
    ledger_name: str
    opening_balance: str
    closing_balance: str
    movement: str


class ArReportModel(BaseModel):
    balances: list[ArLineModel]


class CpaPackModel(BaseModel):
    property_id: str
    pms_source: str
    month: str
    sales: SalesReportModel
    taxes: TaxReportModel
    ar: ArReportModel


# --- QBO push models (P8) ---------------------------------------------------------


class JeLineModel(BaseModel):
    gl_account_code: str
    account_name: str
    posting: str  # "Debit" | "Credit"
    amount: str  # always positive; direction is `posting`
    memo: str


class JePlanModel(BaseModel):
    property_id: str
    business_date: date
    lines: list[JeLineModel]
    total_debits: str
    total_credits: str
    request_hash: str


class PushLedgerRowModel(BaseModel):
    push_id: int
    property_id: str
    business_date: date
    request_hash: str
    qbo_je_id: str | None
    status: str  # "pushed" | "failed" | "stale"
    message: str | None
    pushed_at: datetime


class PushResultModel(BaseModel):
    status: str  # "pushed" | "already-pushed" | "stale" | "failed"
    qbo_je_id: str | None
    message: str | None


class QboPushRequest(BaseModel):
    """POST /api/qbo/push body: `{"property": ..., "date": ...}`."""

    property_id: str = Field(alias="property")
    business_date: date = Field(alias="date")


# --- dataclass -> model converters -----------------------------------------------


def _opt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _line_model(line: reporting.SosLine) -> SosLineModel:
    return SosLineModel(
        major=line.major,
        sub_category=line.sub_category,
        line_item=line.line_item,
        total=str(line.total),
    )


def _labor_variance_model(
    variance: reporting.LaborVariance | None,
) -> LaborVarianceModel | None:
    if variance is None:
        return None
    return LaborVarianceModel(
        lines=[
            LaborVarianceLineModel(
                department=line.department,
                est_cost=_opt(line.est_cost),
                actual_gross=_opt(line.actual_gross),
                employer_burden=_opt(line.employer_burden),
                variance=_opt(line.variance),
                hours_actual=str(line.hours_actual),
                alert=line.alert,
            )
            for line in variance.lines
        ],
        periods=variance.periods,
        est_total=str(variance.est_total),
        actual_total=str(variance.actual_total),
        variance_total=str(variance.variance_total),
        burden_total=str(variance.burden_total),
        alert=variance.alert,
        suppressed_departments=variance.suppressed_departments,
        unpriced_hours=str(variance.unpriced_hours),
    )


def _sos_model(sos: reporting.SosReport) -> SosReportModel:
    return SosReportModel(
        property_id=sos.property_id,
        pms_source=sos.pms_source,
        business_date=sos.business_date,
        date_from=sos.date_from,
        date_to=sos.date_to,
        operated_departments=[
            DeptSectionModel(
                sub_category=dept.sub_category,
                lines=[_line_model(line) for line in dept.lines],
                total=str(dept.total),
            )
            for dept in sos.operated_departments
        ],
        misc_income=[_line_model(line) for line in sos.misc_income],
        misc_income_total=str(sos.misc_income_total),
        total_operating_revenue=str(sos.total_operating_revenue),
        taxes=[_line_model(line) for line in sos.taxes],
        taxes_total=str(sos.taxes_total),
        settlements=[_line_model(line) for line in sos.settlements],
        settlements_total=str(sos.settlements_total),
        other=[_line_model(line) for line in sos.other],
        other_total=str(sos.other_total),
        rooms_segments=[
            SegmentLineModel(
                segment=seg.segment,
                rooms=str(seg.rooms),
                room_revenue=str(seg.room_revenue),
            )
            for seg in sos.rooms_segments
        ],
        statistics=[
            MetricRowModel(
                metric_code=row.metric_code,
                day=_opt(row.day),
                mtd=_opt(row.mtd),
                ytd=_opt(row.ytd),
                day_prior=_opt(row.day_prior),
                mtd_prior=_opt(row.mtd_prior),
                ytd_prior=_opt(row.ytd_prior),
            )
            for row in sos.statistics
        ],
        payroll_expense=[
            LaborLineModel(
                department=line.department,
                hours=str(line.hours),
                ot_hours=str(line.ot_hours),
                est_cost=_opt(line.est_cost),
            )
            for line in sos.payroll_expense
        ],
        payroll_expense_total=str(sos.payroll_expense_total),
        labor_hours_total=str(sos.labor_hours_total),
        labor_ot_hours_total=str(sos.labor_ot_hours_total),
        labor_fte=str(sos.labor_fte) if sos.labor_fte is not None else None,
        labor_suppressed_departments=sos.labor_suppressed_departments,
        labor_unpriced_hours=str(sos.labor_unpriced_hours),
        sick_pay=[
            SickPayLineModel(
                department=line.department,
                hours=str(line.hours),
                cost=_opt(line.cost),
            )
            for line in sos.sick_pay
        ],
        sick_pay_total=str(sos.sick_pay_total),
        sick_suppressed_departments=sos.sick_suppressed_departments,
        sick_unpriced_hours=str(sos.sick_unpriced_hours),
        labor_variance=_labor_variance_model(sos.labor_variance),
    )


def _needs_review_models(entries: list[reporting.NeedsReviewEntry]) -> list[NeedsReviewEntryModel]:
    return [NeedsReviewEntryModel(code=e.code, line_item=e.line_item, notes=e.notes) for e in entries]


def _coverage_model(report: reporting.CoverageReport) -> CoverageReportModel:
    return CoverageReportModel(
        sources=[
            SourceCoverageModel(
                pms_source=src.pms_source,
                financial=FinancialCoverageModel(
                    dictionary_entries=src.financial.dictionary_entries,
                    by_confidence=src.financial.by_confidence,
                    by_review_status=src.financial.by_review_status,
                    needs_review=_needs_review_models(src.financial.needs_review),
                    staged_codes=src.financial.staged_codes,
                    mapped_codes=src.financial.mapped_codes,
                    missing_codes=src.financial.missing_codes,
                    exception_count=src.financial.exception_count,
                    gl_mapped=src.financial.gl_mapped,
                    gl_unmapped_codes=src.financial.gl_unmapped_codes,
                ),
                segments=SegmentCoverageModel(
                    dictionary_entries=src.segments.dictionary_entries,
                    needs_review=_needs_review_models(src.segments.needs_review),
                    staged_codes=src.segments.staged_codes,
                    mapped_codes=src.segments.mapped_codes,
                    unmapped_codes=src.segments.unmapped_codes,
                ),
                statistics=StatisticsCoverageModel(
                    dictionary_entries=src.statistics.dictionary_entries,
                    staged_labels=src.statistics.staged_labels,
                    mapped_labels=src.statistics.mapped_labels,
                    unmapped_labels=src.statistics.unmapped_labels,
                ),
            )
            for src in report.sources
        ]
    )


def _txn_model(txn: reporting.StagedTxn) -> StagedTxnModel:
    return StagedTxnModel(
        stage_id=txn.stage_id,
        pms_source=txn.pms_source,
        business_date=txn.business_date,
        pms_trx_code=txn.pms_trx_code,
        pms_trx_desc=txn.pms_trx_desc,
        amount=str(txn.amount),
        source_file=txn.source_file,
    )


def _cpa_pack_model(pack: reporting.CpaPack) -> CpaPackModel:
    return CpaPackModel(
        property_id=pack.property_id,
        pms_source=pack.pms_source,
        month=pack.month,
        sales=SalesReportModel(
            lines=[
                SalesLineModel(
                    major=line.major,
                    sub_category=line.sub_category,
                    line_item=line.line_item,
                    mtd_amount=str(line.mtd_amount),
                    day_count=line.day_count,
                )
                for line in pack.sales.lines
            ],
            total_operating_revenue=str(pack.sales.total_operating_revenue),
        ),
        taxes=TaxReportModel(
            lines=[
                TaxLineModel(
                    line_item=line.line_item,
                    gl_account_code=line.gl_account_code,
                    mtd_amount=str(line.mtd_amount),
                )
                for line in pack.taxes.lines
            ],
            taxes_total=str(pack.taxes.taxes_total),
            room_revenue_base=str(pack.taxes.room_revenue_base),
        ),
        ar=ArReportModel(
            balances=[
                ArLineModel(
                    ledger_code=line.ledger_code,
                    ledger_name=line.ledger_name,
                    opening_balance=str(line.opening_balance),
                    closing_balance=str(line.closing_balance),
                    movement=str(line.movement),
                )
                for line in pack.ar.balances
            ]
        ),
    )


def _je_plan_model(plan: qbo_push.JePlan) -> JePlanModel:
    return JePlanModel(
        property_id=plan.property_id,
        business_date=plan.business_date,
        lines=[
            JeLineModel(
                gl_account_code=line.gl_account_code,
                account_name=line.account_name,
                posting=line.posting,
                amount=str(line.amount),
                memo=line.memo,
            )
            for line in plan.lines
        ],
        total_debits=str(plan.total_debits),
        total_credits=str(plan.total_credits),
        request_hash=plan.request_hash,
    )


def _ledger_row_model(row: QboPushLedger) -> PushLedgerRowModel:
    return PushLedgerRowModel(
        push_id=row.push_id,
        property_id=row.property_id,
        business_date=row.business_date,
        request_hash=row.request_hash,
        qbo_je_id=row.qbo_je_id,
        status=row.status,
        message=row.message,
        pushed_at=row.pushed_at,
    )


def _unmapped_gl_detail(exc: qbo_push.UnmappedGlError) -> dict[str, list[dict[str, str]]]:
    """The structured 422 body: a curation worklist, not prose."""
    return {
        "unmapped": [
            {"major": major, "sub_category": sub, "line_item": line_item}
            for major, sub, line_item in exc.lines
        ]
    }


def _run_qbo(action: Callable[[], T]) -> T:
    """Run one QBO preview/push action, mapping its failures to HTTP.

    No facts for the day -> 404; facts lacking GL codes -> structured 422 (the
    curation worklist); other ValueErrors (CoA gaps, bad input) -> 422 with the
    message; transport failures reaching QBO (`httpx.HTTPError` — QBO down,
    DNS, refused connection) -> 502 with a clear detail instead of an unhandled
    500 traceback. HTTP-level rejections QBO itself sends are NOT transport
    errors: `push_day` turns those (`QboError`) into a `failed` PushResult.
    """
    try:
        return action()
    except reporting.NoFactsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except qbo_push.UnmappedGlError as exc:
        raise HTTPException(status_code=422, detail=_unmapped_gl_detail(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"cannot reach QBO: {exc}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# --- Router -----------------------------------------------------------------------

router = APIRouter(prefix="/api")


@router.get("/properties")
def properties(
    session: SessionDep, principal: Principal = Depends(require_operator)
) -> list[PropertyInfoModel]:
    """Property picker bootstrap: every IN-SCOPE property with its date window."""
    scope = resolve_scope(principal, session)
    names = {row.property_id: row.name for row in session.query(Property).all()}
    return [
        PropertyInfoModel(
            property_id=p.property_id,
            pms_source=p.pms_source,
            first_date=p.first_date,
            last_date=p.last_date,
            name=names.get(p.property_id),
        )
        for p in reporting.list_properties(session)
        if scope.allows_property(p.property_id)
    ]


@router.get("/sos", dependencies=[Depends(require_property_access)])
def sos(
    session: SessionDep,
    property_id: Annotated[str, Query(alias="property")],
    business_date: Annotated[date | None, Query(alias="date")] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> SosReportModel:
    """Summary Operating Statement: `date` (single day) XOR `from`+`to` (range)."""
    single = business_date is not None
    ranged = date_from is not None or date_to is not None
    if single == ranged:
        raise HTTPException(
            status_code=422, detail="pass exactly one of date, or both from and to"
        )
    if ranged and (date_from is None or date_to is None):
        raise HTTPException(status_code=422, detail="from and to must be given together")
    report = _run(
        lambda: reporting.summary_operating_statement(
            session,
            property_id=property_id,
            business_date=business_date,
            date_from=date_from,
            date_to=date_to,
        )
    )
    return _sos_model(report)


class LaborDayModel(BaseModel):
    business_date: str
    hours: Decimal
    ot_hours: Decimal
    est_cost: Decimal
    rooms_occupied: Decimal | None
    revenue: Decimal | None
    # Hours per department on this day. HOURS ONLY — a per-day per-department
    # cost would re-derive a solo employee's rate, which is the whole point of
    # the suppression the cost fields carry.
    department_hours: dict[str, Decimal]


class LaborDepartmentModel(BaseModel):
    department: str
    hours: Decimal
    ot_hours: Decimal
    # None when the department has fewer than two PRICED employees on some day
    # that carried cost — cost / hours would re-derive an individual's rate.
    est_cost: Decimal | None
    target_hours: Decimal | None


class LaborAnalyticsModel(BaseModel):
    property_id: str
    date_from: str
    date_to: str
    days: list[LaborDayModel]
    departments: list[LaborDepartmentModel]
    hours_total: Decimal
    ot_hours_total: Decimal
    cost_total: Decimal
    revenue_total: Decimal
    rooms_total: Decimal
    fte: Decimal | None
    suppressed_departments: int
    unpriced_hours: Decimal


@router.get("/labor/analytics", dependencies=[Depends(require_property_access)])
def labor_analytics(
    session: SessionDep,
    property_id: Annotated[str, Query(alias="property")],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
) -> LaborAnalyticsModel:
    """Labor cost and hours over a window, per day and per department.

    Open to every operator, because everything it returns is a DEPARTMENT
    aggregate carrying the statement's own suppression — the same numbers
    Schedule 14 already shows, arranged for a staffing decision rather than a
    filing.
    """
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if (date_to - date_from).days > 400:
        # A guard, not a policy: the day series costs one query set per day.
        raise HTTPException(status_code=422, detail="window must be 400 days or fewer")
    a = _run(
        lambda: reporting.labor_analytics(session, property_id, date_from, date_to)
    )
    return LaborAnalyticsModel(
        property_id=a.property_id,
        date_from=a.date_from.isoformat(),
        date_to=a.date_to.isoformat(),
        days=[
            LaborDayModel(
                business_date=d.business_date.isoformat(),
                hours=d.hours, ot_hours=d.ot_hours, est_cost=d.est_cost,
                rooms_occupied=d.rooms_occupied, revenue=d.revenue,
                department_hours=d.department_hours,
            )
            for d in a.days
        ],
        departments=[
            LaborDepartmentModel(
                department=x.department, hours=x.hours, ot_hours=x.ot_hours,
                est_cost=x.est_cost, target_hours=x.target_hours,
            )
            for x in a.departments
        ],
        hours_total=a.hours_total,
        ot_hours_total=a.ot_hours_total,
        cost_total=a.cost_total,
        revenue_total=a.revenue_total,
        rooms_total=a.rooms_total,
        fte=a.fte,
        suppressed_departments=a.suppressed_departments,
        unpriced_hours=a.unpriced_hours,
    )


# --- Core performance statistics (issue #9) --------------------------------------


class CoreMetricsModel(BaseModel):
    start: str
    end: str
    rooms_available: str
    rooms_sold: str
    adr_rooms_sold: str
    room_revenue: str
    total_revenue: str
    occupancy: str | None
    adr: str | None
    revpar: str | None
    trevpar: str | None
    adr_room_basis: str


class ReconLineModel(BaseModel):
    computed: str | None
    ingested: str | None
    agrees: bool | None


class TrendPairModel(BaseModel):
    current: str | None
    prior: str | None
    delta_pct: str | None


class RollingStatModel(BaseModel):
    avg: str | None
    stdev: str | None
    n: int


class TrendsModel(BaseModel):
    anchor: str
    wow: dict[str, TrendPairModel]
    mtd: dict[str, str | None]
    rolling_30: dict[str, RollingStatModel]
    dow: dict[str, TrendPairModel]


class PerformanceResponse(BaseModel):
    property_id: str
    adr_room_basis: str
    period: str | None
    start: str
    end: str
    current: CoreMetricsModel
    prior_period: CoreMetricsModel | None
    prior_year: CoreMetricsModel | None
    prior_period_delta_pct: dict[str, str | None]
    prior_year_delta_pct: dict[str, str | None]
    reconciliation: dict[str, ReconLineModel]
    trends: TrendsModel
    days_excluded: int


class RoomRevenueTxnsModel(BaseModel):
    """Drill-through payload for the room-revenue metric: the staged PMS
    transactions behind the operating statement's Rooms revenue line."""

    property_id: str
    date_from: date
    date_to: date
    transactions: list[StagedTxnModel]


# The room-revenue metric drills through to the operating statement's Rooms
# revenue financial line (Schedule 1 accommodation revenue). Its (major,
# sub_category, line_item) triple is stable across PMS sources — every mapping
# routes accommodation revenue here (see mapping/opera.yaml, mapping/autoclerk.yaml).
_ROOM_REVENUE_LINE = ("Operated Departments", "Rooms", "Room Revenue")


def _dec(value: Decimal | None) -> str | None:
    """Optional money/ratio -> exact string, mirroring `_opt` for the perf models."""
    return None if value is None else str(value)


def _core_metrics_model(m: CoreMetrics) -> CoreMetricsModel:
    return CoreMetricsModel(
        start=m.start.isoformat(),
        end=m.end.isoformat(),
        rooms_available=str(m.rooms_available),
        rooms_sold=str(m.rooms_sold),
        adr_rooms_sold=str(m.adr_rooms_sold),
        room_revenue=str(m.room_revenue),
        total_revenue=str(m.total_revenue),
        occupancy=_dec(m.occupancy),
        adr=_dec(m.adr),
        revpar=_dec(m.revpar),
        trevpar=_dec(m.trevpar),
        adr_room_basis=m.adr_room_basis,
    )


def _recon_line_model(line: ReconLine) -> ReconLineModel:
    return ReconLineModel(
        computed=_dec(line.computed), ingested=_dec(line.ingested), agrees=line.agrees
    )


def _trend_pair_model(pair: TrendPair) -> TrendPairModel:
    return TrendPairModel(
        current=_dec(pair.current), prior=_dec(pair.prior), delta_pct=_dec(pair.delta_pct)
    )


def _rolling_stat_model(stat: RollingStat) -> RollingStatModel:
    return RollingStatModel(avg=_dec(stat.avg), stdev=_dec(stat.stdev), n=stat.n)


def _trends_model(t: Trends) -> TrendsModel:
    return TrendsModel(
        anchor=t.anchor.isoformat(),
        wow={k: _trend_pair_model(v) for k, v in t.wow.items()},
        mtd={k: _dec(v) for k, v in t.mtd.items()},
        rolling_30={k: _rolling_stat_model(v) for k, v in t.rolling_30.items()},
        dow={k: _trend_pair_model(v) for k, v in t.dow.items()},
    )


def _performance_response(
    property_id: str,
    basis: str,
    period: str | None,
    start: date,
    end: date,
    cmp: Comparison,
    recon: dict[str, ReconLine],
    trend: Trends,
    excluded: int,
) -> PerformanceResponse:
    return PerformanceResponse(
        property_id=property_id,
        adr_room_basis=basis,
        period=period,
        start=start.isoformat(),
        end=end.isoformat(),
        current=_core_metrics_model(cmp.current),
        prior_period=None if cmp.prior_period is None else _core_metrics_model(cmp.prior_period),
        prior_year=None if cmp.prior_year is None else _core_metrics_model(cmp.prior_year),
        prior_period_delta_pct={k: _dec(v) for k, v in cmp.prior_period_delta_pct.items()},
        prior_year_delta_pct={k: _dec(v) for k, v in cmp.prior_year_delta_pct.items()},
        reconciliation={k: _recon_line_model(v) for k, v in recon.items()},
        trends=_trends_model(trend),
        days_excluded=excluded,
    )


@router.get("/performance", dependencies=[Depends(require_property_access)])
def get_performance(
    session: SessionDep,
    property: Annotated[str, Query(alias="property")],
    from_: Annotated[date | None, Query(alias="from")] = None,
    to: Annotated[date | None, Query(alias="to")] = None,
    period: Annotated[str | None, Query()] = None,
) -> PerformanceResponse:
    """Core performance statistics for a window: current-window occupancy/ADR/
    RevPAR/TRevPAR with prior-period + prior-year comparisons, a PMS-KPI
    reconciliation, and operator trend bases anchored on the window end.
    `days_excluded` is the window length minus its data-complete days.

    The window is given EITHER by `from`+`to` (an explicit range) OR by
    `period=YYYY-Pnn` (resolved via the property's fiscal calendar) — exactly
    one form, never both nor neither. The response echoes `period` when the
    fiscal form was used, `null` for an explicit range.

    Fail-loud service exceptions (inventory not configured, ADR basis missing its
    segment data) map to 409 — a request the data cannot yet answer, distinct from
    a malformed request (422). A `period` on a property with no fiscal calendar is
    likewise 409; a malformed/out-of-range period key is 422.
    """
    ranged = from_ is not None and to is not None
    if ranged == (period is not None):
        # Both forms, or neither, or a half-given range (only from_ or only to).
        raise HTTPException(
            status_code=422,
            detail="pass exactly one of period, or both from and to",
        )
    resolved_period: str | None
    if period is not None:
        try:
            cfg = require_config(_fiscal_config(session, property))
        except FiscalCalendarNotConfigured as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            start, end = resolve_period(cfg, period)
        except ValueError as exc:  # malformed / out-of-range period key
            raise HTTPException(status_code=422, detail=str(exc)) from None
        resolved_period = period
    else:
        assert from_ is not None and to is not None  # narrowed by `ranged`
        if to < from_:
            raise HTTPException(status_code=422, detail="'to' must not precede 'from'")
        start, end, resolved_period = from_, to, None
    basis = _adr_room_basis(session, property)
    try:
        cmp = compare(session, property, start, end, basis=basis)
        recon = reconciliation(session, property, start, end, cmp.current)
        trend = trends(session, property, end, basis=basis)
        # TODO(#25 rebase): add InventoryInconsistent to this except tuple once
        # PR #25 lands it on this branch (it does not exist here yet).
        excluded = (end - start).days + 1 - len(complete_days(session, property, start, end))
    except (InventoryNotConfigured, AdrBasisUnavailable) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return _performance_response(
        property, basis, resolved_period, start, end, cmp, recon, trend, excluded
    )


@router.get("/sos/line/transactions", dependencies=[Depends(require_property_access)])
def sos_line_transactions(
    session: SessionDep,
    property_id: Annotated[str, Query(alias="property")],
    major: str,
    sub_category: Annotated[str, Query(alias="sub")],
    line_item: str,
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
) -> list[StagedTxnModel]:
    """Drill-through: the staged PMS transactions behind one SOS financial line."""
    txns = _run(
        lambda: reporting.line_transactions(
            session,
            property_id=property_id,
            major=major,
            sub_category=sub_category,
            line_item=line_item,
            date_from=date_from,
            date_to=date_to,
        )
    )
    return [_txn_model(t) for t in txns]


@router.get(
    "/performance/room-revenue/transactions",
    dependencies=[Depends(require_property_access)],
)
def performance_room_revenue_transactions(
    session: SessionDep,
    property_id: Annotated[str, Query(alias="property")],
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
) -> RoomRevenueTxnsModel:
    """Drill-through for the room-revenue performance metric.

    Reuses the SOS financial-fact -> stage machinery (`line_transactions`) for the
    Rooms revenue line, so every dollar of the metric's `room_revenue` traces to a
    staged PMS transaction, consistent with the operating statement. Other metrics
    drill to their own source stage: occupancy/ADR -> statistic facts; labor ->
    timecards. This endpoint implements the revenue (room-revenue) drill-through.
    """
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="from must not be after to")
    major, sub_category, line_item = _ROOM_REVENUE_LINE
    txns = _run(
        lambda: reporting.line_transactions(
            session,
            property_id=property_id,
            major=major,
            sub_category=sub_category,
            line_item=line_item,
            date_from=date_from,
            date_to=date_to,
        )
    )
    return RoomRevenueTxnsModel(
        property_id=property_id,
        date_from=date_from,
        date_to=date_to,
        transactions=[_txn_model(t) for t in txns],
    )


@router.get("/coverage")
def coverage(session: SessionDep, edition: Annotated[int, Query(ge=1)] = 12) -> CoverageReportModel:
    """Mapping coverage and confidence report, one section per PMS source."""
    report = _run(
        lambda: reporting.coverage_report(
            session,
            edition=edition,
            segments_path=_SEGMENTS_YAML,
            statistics_path=_STATISTICS_YAML,
        )
    )
    return _coverage_model(report)


@router.get("/cpa-pack", dependencies=[Depends(require_property_access)])
def cpa_pack_endpoint(
    session: SessionDep,
    property_id: Annotated[str, Query(alias="property")],
    month: str,
) -> CpaPackModel:
    """CPA monthly pack (sales, tax, A/R) for one property and 'YYYY-MM' month.

    An empty month for a known property is a clean empty pack (200). An unknown
    property is a plain ValueError from `cpa_pack` (deliberately NOT a
    NoFactsError — see its docstring), mapped HERE to 404 by its message: it is
    a missing resource, not a malformed request, and must not read as a 422.
    A malformed month stays 422 with the message.
    """
    try:
        pack = reporting.cpa_pack(session, property_id=property_id, month=month)
    except ValueError as exc:
        status = 404 if str(exc).startswith("unknown property") else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _cpa_pack_model(pack)


@router.get("/qbo/status")
def qbo_status(
    session: SessionDep,
    principal: Principal = Depends(require_operator),
    property_id: Annotated[str | None, Query(alias="property")] = None,
    month: Annotated[str | None, Query()] = None,
) -> list[PushLedgerRowModel]:
    """Push-ledger rows, optionally filtered by property and/or 'YYYY-MM' month.

    Rows are further filtered to the caller's property scope (no cross-property leak).
    """
    scope = resolve_scope(principal, session)
    rows = _run(
        lambda: qbo_push.push_ledger_rows(session, property_id=property_id, month=month)
    )
    return [
        _ledger_row_model(row) for row in rows if scope.allows_property(row.property_id)
    ]


@router.get("/qbo/preview", dependencies=[Depends(require_property_access)])
def qbo_preview(
    session: SessionDep,
    property_id: Annotated[str, Query(alias="property")],
    business_date: Annotated[date, Query(alias="date")],
) -> JePlanModel:
    """The balanced JE plan for one property + business date; posts nothing."""
    plan = _run_qbo(
        lambda: qbo_push.build_journal_entry(
            session, property_id=property_id, business_date=business_date
        )
    )
    return _je_plan_model(plan)


@router.post("/qbo/push")
def qbo_push_endpoint(
    session: SessionDep,
    client: QboClientDep,
    body: QboPushRequest,
    principal: Principal = Depends(require_operator),
) -> PushResultModel:
    """Push one property+date JE to QBO — the portal's first write action.

    Idempotent per `push_day`'s ledger contract (re-pushing identical content
    is `already-pushed`; changed facts mark the row `stale` and refuse).
    `push_day` commits the session itself — its documented contract — so the
    outcome survives even though the per-request session just closes.

    Property scope is enforced from the request BODY (`body.property_id`), not a
    query param, so the check lives here rather than in `require_property_access`
    (which reads `Query(alias="property")`). Enforced BEFORE the push so an
    out-of-scope caller 403s without ever touching QBO.
    """
    scope = resolve_scope(principal, session)
    if not scope.allows_property(body.property_id):
        raise HTTPException(status_code=403, detail="property out of scope")
    result = _run_qbo(
        lambda: qbo_push.push_day(
            session, client, property_id=body.property_id, business_date=body.business_date
        )
    )
    return PushResultModel(
        status=result.status, qbo_je_id=result.qbo_je_id, message=result.message
    )
