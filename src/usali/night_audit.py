"""Night-audit flow: required-report checklist, ledger checks, and the roll.

The system's dates are otherwise DERIVED from the data (each report carries its
own business date; attendance uses the property-local 04:00 cutoff). The night
audit makes the operating date EXPLICIT: `NightAuditState.current_business_date`
is the day awaiting its audit. Uploading the night's required reports fills the
checklist (read from `ingestion_coverage` — the same rows `process_file`
records); the ledger checks then verify the close against the last one; and the
ROLL advances the date by one, allowed only inside the property-local roll
window (00:00–05:00, hardcoded for now — becomes onboarding config later).

Required report sets are keyed by pms_source for now. The onboarding milestone
moves this (and the per-PMS upload formats — XML/XLS alongside PDF) into
per-property configuration; keep this registry the single place that decides.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from usali.attendance import business_date_for
from usali.models import (
    IngestionCoverage,
    NightAuditState,
    PmsDailySegmentStage,
    Property,
    UsaliFinancialFact,
    UsaliLedgerBalanceFact,
    UsaliStatisticFact,
)

# pms_source -> ordered (report_type, display label). Order is the upload order
# shown to the auditor; completeness is a set test.
REQUIRED_REPORTS: dict[str, tuple[tuple[str, str], ...]] = {
    "OPERA": (
        ("trial_balance", "Trial Balance"),
        ("manager_flash", "Manager Flash"),
        ("market_stats", "Market Code Statistics"),
    ),
    "AUTOCLERK": (
        ("transaction_summary", "Transaction Summary"),
        ("manager_report", "Manager Report"),
        ("rate_plan", "Revenue by Rate Plan"),
    ),
    # SkyTouch emails ONE bundled pack; both required reports arrive inside it.
    "SKYTOUCH": (
        ("hotel_journal", "Hotel Journal Summary"),
        ("hotel_statistics", "Hotel Statistics"),
    ),
}

# pms_source -> single-upload label. A PMS listed here takes ONE drop (the
# bundled night-audit pack, split server-side via process_pack) instead of
# per-report uploads; every recognized section fills its own slot.
PACK_UPLOAD: dict[str, str] = {
    "SKYTOUCH": "Standard Audit Pack (one PDF, split report-by-report)",
}

ROLL_WINDOW_START_HOUR = 0  # 00:00 property-local
ROLL_WINDOW_END_HOUR = 5    # exclusive: rolls allowed strictly before 05:00

# Ledger identity: HOTEL_BALANCE is the sum of the four sub-ledgers.
_SUB_LEDGERS = ("GUEST_LEDGER", "AR_LEDGER", "DEPOSIT_LEDGER", "PACKAGE_LEDGER")
_TOL = Decimal("0.01")


def _fmt_rooms(value: Decimal) -> str:
    """Rooms are COUNTS: whole numbers on every surface."""
    return str(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fmt_money(value: Decimal) -> str:
    """Money: two decimal places on every surface."""
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class LedgerCheck:
    name: str
    status: str  # "pass" | "fail" | "skipped"
    detail: str
    delta: str | None = None
    # Present on a FAILED cross-night check: what the dashboard may correct
    # directly (the stored prior close) and the value that zeroes the residual.
    adjust: dict[str, str] | None = None


def get_or_init_state(session: Session, prop: Property) -> NightAuditState:
    """Lazy get-or-create (the property-config upsert idiom: ORM path so the
    org_id before_flush stamp applies). Initial date = the day AFTER the last
    fact on record — that day's audit already happened, by definition — or the
    property-local business date today when the property has no data yet."""
    state = session.get(NightAuditState, prop.property_id)
    if state is not None:
        return state
    last_fact = session.execute(
        select(func.max(UsaliFinancialFact.business_date)).where(
            UsaliFinancialFact.property_id == prop.property_id
        )
    ).scalar()
    initial = (
        last_fact + timedelta(days=1)
        if last_fact is not None
        else business_date_for(datetime.now(UTC), prop.timezone)
    )
    state = NightAuditState(property_id=prop.property_id, current_business_date=initial)
    session.add(state)
    session.flush()
    return state


def slot_status(
    session: Session, property_id: str, day: date, pms_source: str
) -> list[dict[str, object]]:
    required = REQUIRED_REPORTS.get(pms_source.upper(), ())
    landed = {
        r
        for (r,) in session.execute(
            select(IngestionCoverage.report_type).where(
                IngestionCoverage.property_id == property_id,
                IngestionCoverage.business_date == day,
            )
        )
    }
    return [
        {"report_type": rt, "label": label, "landed": rt in landed}
        for rt, label in required
    ]


def _balances(session: Session, property_id: str, day: date) -> dict[str, Decimal]:
    rows = session.execute(
        select(UsaliLedgerBalanceFact.ledger_code, UsaliLedgerBalanceFact.amount).where(
            UsaliLedgerBalanceFact.property_id == property_id,
            UsaliLedgerBalanceFact.business_date == day,
        )
    ).all()
    return {code: Decimal(str(amount)) for code, amount in rows}


def ledger_checks(session: Session, property_id: str, day: date) -> list[LedgerCheck]:
    """The 'balances are zero as per the last' verification, from the trial
    balance's ledger block. Two zero-checks, each honest about absent data
    (AutoClerk reports carry no ledger block; a first night has no prior close):

    * identity — GUEST + AR + DEPOSIT + PACKAGE − HOTEL_BALANCE == 0 today.
    * AR roll-forward — prior AR close + today's charges − payments − today's
      AR close == 0.
    """
    today = _balances(session, property_id, day)
    checks: list[LedgerCheck] = []

    if not today:
        return [
            LedgerCheck(
                name="ledger_block",
                status="skipped",
                detail="no ledger balances on file for this date (report family "
                "carries no ledger block, or the trial balance has not landed)",
            )
        ]

    if "HOTEL_BALANCE" in today and all(k in today for k in _SUB_LEDGERS):
        delta = sum((today[k] for k in _SUB_LEDGERS), Decimal("0")) - today["HOTEL_BALANCE"]
        checks.append(
            LedgerCheck(
                name="balance_identity",
                status="pass" if abs(delta) <= _TOL else "fail",
                detail="guest + AR + deposit + package vs hotel balance",
                delta=_fmt_money(delta),
            )
        )
    else:
        checks.append(
            LedgerCheck(
                name="balance_identity", status="skipped",
                detail="ledger block incomplete: missing one of the four sub-ledgers "
                "or the hotel balance",
            )
        )

    prior = _balances(session, property_id, day - timedelta(days=1))
    if "AR_LEDGER" in prior and "AR_LEDGER" in today:
        charges = today.get("AR_CHARGES", Decimal("0"))
        payments = today.get("AR_PAYMENTS", Decimal("0"))
        delta = prior["AR_LEDGER"] + charges - payments - today["AR_LEDGER"]
        failed = abs(delta) > _TOL
        checks.append(
            LedgerCheck(
                name="ar_rollforward",
                status="fail" if failed else "pass",
                detail="prior AR close + charges − payments vs today's AR close",
                delta=_fmt_money(delta),
                # Direct-edit affordance: correcting the PRIOR close to
                # (stored − Δ) zeroes the residual. The API records old → new
                # + a mandatory reason in night_audit_adjustment.
                adjust={
                    "business_date": (day - timedelta(days=1)).isoformat(),
                    "ledger_code": "AR_LEDGER",
                    "stored": _fmt_money(prior["AR_LEDGER"]),
                    "suggested": _fmt_money(prior["AR_LEDGER"] - delta),
                } if failed else None,
            )
        )
    else:
        checks.append(
            LedgerCheck(
                name="ar_rollforward", status="skipped",
                detail="no prior-day AR close on file to roll forward from",
            )
        )
    return checks


def roll_window(prop: Property, now: datetime | None = None) -> dict[str, object]:
    """Whether the property-local roll window (00:00–05:00) is open right now."""
    at = (now or datetime.now(UTC)).astimezone(ZoneInfo(prop.timezone))
    open_now = ROLL_WINDOW_START_HOUR <= at.hour < ROLL_WINDOW_END_HOUR
    return {
        "open": open_now,
        "hours": f"{ROLL_WINDOW_START_HOUR:02d}:00–{ROLL_WINDOW_END_HOUR:02d}:00",
        "timezone": prop.timezone,
        "local_time": at.strftime("%H:%M"),
    }


# ---- rooms & revenue by market code (Opera, for now) -----------------------
# The reconciliation the roll gates on: Σ per-market-code rooms must equal the
# Manager Flash's occupied-rooms total, and Σ per-code room revenue must equal
# the Trial Balance's Rooms line. Reads ONLY the promoted fact tables — the
# same rows every report page uses. AutoClerk's rate-plan analog comes later.

_SEGMENT_SOURCES = frozenset({"OPERA"})


@dataclass
class _CodeRow:
    """One raw market-code line while assembling the reconciliation table."""

    code: str
    description: str
    rooms: Decimal = Decimal("0")
    room_revenue: Decimal = Decimal("0")


def segment_reconciliation(
    session: Session, property_id: str, day: date, pms_source: str
) -> dict[str, object] | None:
    """The RAW market-code table (the report's own lines, not the USALI
    rollup): every code the Market Code Statistics report printed, with its
    rooms and room revenue, reconciled against the Manager Flash occupied
    total and the Trial Balance Rooms line. The report's own TOTAL row is
    shown too — promotion (segment_promote) enforces Σ codes == TOTAL, so a
    corrected table re-promotes cleanly. None when this PMS has no segment
    reconciliation (yet); skipped while a needed report has not landed."""
    if pms_source.upper() not in _SEGMENT_SOURCES:
        return None

    stage_rows = session.execute(
        select(PmsDailySegmentStage).where(
            PmsDailySegmentStage.property_id == property_id,
            PmsDailySegmentStage.business_date == day,
            PmsDailySegmentStage.period_label == "DAY",
        ).order_by(PmsDailySegmentStage.segment_code)
    ).scalars().all()

    rooms_ref = session.execute(
        select(UsaliStatisticFact.value).where(
            UsaliStatisticFact.property_id == property_id,
            UsaliStatisticFact.business_date == day,
            UsaliStatisticFact.metric_code == "ROOMS_OCCUPIED",
            UsaliStatisticFact.period == "DAY",
            UsaliStatisticFact.is_prior_year.is_(False),
        )
    ).scalar()
    revenue_ref = session.execute(
        select(func.sum(UsaliFinancialFact.amount)).where(
            UsaliFinancialFact.property_id == property_id,
            UsaliFinancialFact.business_date == day,
            UsaliFinancialFact.usali_sub_category == "Rooms",
        )
    ).scalar()

    if not stage_rows or rooms_ref is None or revenue_ref is None:
        missing = []
        if not stage_rows:
            missing.append("market-code statistics")
        if rooms_ref is None:
            missing.append("manager flash occupied total")
        if revenue_ref is None:
            missing.append("trial-balance rooms line")
        return {
            "status": "skipped",
            "detail": "awaiting " + ", ".join(missing),
            "rows": [], "rooms_total": None, "revenue_total": None,
            "rooms_ref": None, "revenue_ref": None,
            "rooms_delta": None, "revenue_delta": None,
            "report_total_rooms": None, "report_total_revenue": None,
        }

    # collapse the (code, measure) stage rows into one row per code
    by_code: dict[str, _CodeRow] = {}
    report_totals: dict[str, Decimal | None] = {"ROOMS": None, "ROOM_REVENUE": None}
    for r in stage_rows:
        if r.segment_code == "TOTAL":
            report_totals[r.measure] = Decimal(str(r.value))
            continue
        row = by_code.setdefault(r.segment_code, _CodeRow(
            code=r.segment_code,
            description=(r.segment_desc or "").removesuffix(f" - {r.segment_code}"),
        ))
        if r.measure == "ROOMS":
            row.rooms = Decimal(str(r.value))
        elif r.measure == "ROOM_REVENUE":
            row.room_revenue = Decimal(str(r.value))

    rooms_total = sum((r.rooms for r in by_code.values()), Decimal("0"))
    revenue_total = sum((r.room_revenue for r in by_code.values()), Decimal("0"))
    rooms_delta = rooms_total - Decimal(str(rooms_ref))
    revenue_delta = revenue_total - Decimal(str(revenue_ref))
    ok = abs(rooms_delta) <= _TOL and abs(revenue_delta) <= _TOL
    return {
        "status": "pass" if ok else "fail",
        "detail": "Σ market-code rooms vs Manager Flash · Σ market-code revenue vs Trial Balance",
        "rows": [
            {"code": r.code, "description": r.description,
             "rooms": _fmt_rooms(r.rooms), "room_revenue": _fmt_money(r.room_revenue)}
            for r in by_code.values()
        ],
        "rooms_total": _fmt_rooms(rooms_total), "revenue_total": _fmt_money(revenue_total),
        "rooms_ref": _fmt_rooms(Decimal(str(rooms_ref))),
        "revenue_ref": _fmt_money(Decimal(str(revenue_ref))),
        "rooms_delta": _fmt_rooms(rooms_delta), "revenue_delta": _fmt_money(revenue_delta),
        "report_total_rooms": (
            None if report_totals["ROOMS"] is None else _fmt_rooms(report_totals["ROOMS"])
        ),
        "report_total_revenue": (
            None if report_totals["ROOM_REVENUE"] is None
            else _fmt_money(report_totals["ROOM_REVENUE"])
        ),
    }
