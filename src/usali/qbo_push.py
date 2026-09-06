"""Idempotent QBO push orchestration (P8 Task 4).

The journal-entry builder that used to live here moved to
`usali.gl_posting.build_pms_daily_plan` in OH-27 Task 6 (economics
unchanged — the bucketing, balancing, and canonical `request_hash` are
documented there). `build_journal_entry` below delegates to it and keeps
this module's documented contract: `NoFactsError` on an empty day, and —
until Task 10 retires the fact path — the `mapping/qbo_accounts.yaml`
chart as a fallback when the org has no `gl_account` rows.

`request_hash`'s 50-char prefix doubles as Intuit's `requestid`
idempotency key, and `qbo_push_ledger` records it per (property,
business_date) so `push_day` can distinguish "already pushed this exact
JE" (no-op) from "facts changed after pushing" (mark stale, refuse — the
posted JE needs manual correction first; automated void/amend is out of
scope for P8).

Writes: `push_day` commits the session itself — each date's ledger outcome
must survive later dates failing (`push_month` outcomes are independent).
"""

from datetime import UTC, date, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali import gl_posting
# The builder types moved to the engine in OH-27 Task 6; re-exported so
# P8-era callers keep working (the month_bounds re-export precedent below).
from usali.gl_posting import (  # noqa: F401  (re-exports)
    JeLine,
    JePlan,
    UnmappedGlError,
)
from usali.models import GlAccount, QboPushLedger, UsaliFinancialFact
from usali.qbo_client import QboClient, QboError, QboUnreachable
# month_bounds lives in usali.reporting (the CPA pack shares it); re-exported here
# so existing `qbo_push.month_bounds` callers keep working.
from usali.reporting import NoFactsError, month_bounds

_DEFAULT_ACCOUNTS = Path(__file__).resolve().parents[2] / "mapping" / "qbo_accounts.yaml"
# The JE balancing account (see the mapping/qbo_accounts.yaml header). Builder-only:
# no dictionary entry maps to it — it absorbs each day's un-settled remainder.
_BALANCING_ACCOUNT = "1210"
_REQUESTID_MAX = 50  # Intuit's requestid length cap

PushStatus = Literal["pushed", "already-pushed", "stale", "failed"]


@dataclass(frozen=True)
class PushResult:
    status: PushStatus
    qbo_je_id: str | None
    message: str | None


def _load_account_names(accounts_path: Path) -> dict[str, str]:
    """Chart-of-accounts code -> name from mapping/qbo_accounts.yaml."""
    raw = yaml.safe_load(accounts_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{accounts_path} must be a YAML list of accounts")
    return {str(entry["account_code"]): str(entry["name"]) for entry in raw}


def build_journal_entry(
    session: Session,
    *,
    property_id: str,
    business_date: date,
    accounts_path: Path = _DEFAULT_ACCOUNTS,
) -> JePlan:
    """Build the balanced JE plan for one property + business date.

    Thin delegation to `gl_posting.build_pms_daily_plan` (the moved builder),
    preserving this function's documented contract: raises `NoFactsError` when
    the day has no facts, `UnmappedGlError` when any fact in scope lacks a GL
    account code, and `ValueError` when a fact's GL code (or the balancing
    account) is missing from the chart of accounts.

    Transitional shim (removed in Task 10): when the org has no `gl_account`
    rows yet, account names fall back to `accounts_path` (the pre-OH27 YAML
    chart) and the balancing line to `_BALANCING_ACCOUNT` — so pushes for
    orgs that never seeded a DB chart keep behaving exactly as before.
    """
    if session.scalar(select(GlAccount).limit(1)) is None:
        plan = gl_posting.build_pms_daily_plan(
            session,
            property_id,
            business_date,
            account_names=_load_account_names(accounts_path),
            balancing_account=_BALANCING_ACCOUNT,
        )
    else:
        plan = gl_posting.build_pms_daily_plan(session, property_id, business_date)
    if plan is None:
        raise NoFactsError(
            f"no financial facts for property {property_id} on {business_date}"
        )
    return plan


def journal_entry_body(plan: JePlan) -> dict[str, Any]:
    """Intuit-shaped JournalEntry create body for one plan.

    Amounts are serialized as strings (`str(Decimal)`) — exact through JSON,
    never a binary float; the mock (like real QBO) accepts either.
    """
    return {
        "TxnDate": plan.business_date.isoformat(),
        "PrivateNote": f"USALI daily push {plan.property_id} {plan.business_date.isoformat()}",
        "Line": [
            {
                "Amount": str(line.amount),
                "DetailType": "JournalEntryLineDetail",
                "Description": line.memo,
                "JournalEntryLineDetail": {
                    "PostingType": line.posting,
                    "AccountRef": {"value": line.gl_account_code, "name": line.account_name},
                },
            }
            for line in plan.lines
        ],
    }


def push_day(
    session: Session, client: QboClient, *, property_id: str, business_date: date
) -> PushResult:
    """Push one property+date JE idempotently, recording the outcome.

    Consults `qbo_push_ledger` first: an identical already-pushed JE is a
    no-op (`already-pushed`); a pushed JE whose facts have since changed is
    marked `stale` and NOT re-pushed (a stale row whose facts revert to the
    pushed hash is restored to `pushed`). `failed` rows are retried. Commits
    the session so each date's outcome survives later failures.

    Concurrency: two simultaneous pushes of the same (property, date) are
    arbitrated by the table's unique constraint — the loser's INSERT raises
    IntegrityError on commit, loudly. QBO-side it is safe either way: both
    attempts derive the same request_hash, so Intuit's requestid replay
    guarantees at most one journal entry exists.
    """
    plan = build_journal_entry(session, property_id=property_id, business_date=business_date)
    row = session.scalar(
        select(QboPushLedger).where(
            QboPushLedger.property_id == property_id,
            QboPushLedger.business_date == business_date,
        )
    )
    if row is not None and row.status == "pushed":
        if row.request_hash == plan.request_hash:
            return PushResult(status="already-pushed", qbo_je_id=row.qbo_je_id, message=None)
        row.status = "stale"
        row.message = (
            f"facts changed after push: pushed hash {row.request_hash[:12]} != "
            f"current {plan.request_hash[:12]}; correct the QBO JE manually"
        )[:500]
        session.commit()
        return PushResult(status="stale", qbo_je_id=row.qbo_je_id, message=row.message)
    if row is not None and row.status == "stale":
        if row.request_hash == plan.request_hash:
            # Facts reverted to exactly what was pushed: the JE is correct again.
            row.status = "pushed"
            row.message = None
            session.commit()
            return PushResult(status="already-pushed", qbo_je_id=row.qbo_je_id, message=None)
        return PushResult(status="stale", qbo_je_id=row.qbo_je_id, message=row.message)

    # New day, or retrying a failed attempt.
    now = datetime.now(UTC)
    if row is None:
        row = QboPushLedger(
            property_id=property_id,
            business_date=business_date,
            request_hash=plan.request_hash,
            status="failed",
            pushed_at=now,
        )
        session.add(row)
    row.request_hash = plan.request_hash
    row.pushed_at = now
    try:
        je_id = client.post_journal_entry(
            journal_entry_body(plan), request_id=plan.request_hash[:_REQUESTID_MAX]
        )
    except QboUnreachable:
        # Not a per-date outcome: the endpoint is down, so every remaining
        # date in this run would record an identical `failed` row for one
        # network blip. Let it escape to the caller, which aborts the push —
        # the behaviour that held while this was a bare httpx error, kept
        # explicit now that the client wraps transport failures (2026-08-31).
        raise
    except QboError as exc:
        row.status = "failed"
        row.qbo_je_id = None
        row.message = str(exc)[:500]
        session.commit()
        return PushResult(status="failed", qbo_je_id=None, message=row.message)
    row.status = "pushed"
    row.qbo_je_id = je_id
    row.message = None
    session.commit()
    return PushResult(status="pushed", qbo_je_id=je_id, message=None)


def fact_dates(session: Session, *, property_id: str, month: str) -> list[date]:
    """Business dates in the month that have financial facts, ascending."""
    start, end = month_bounds(month)
    return list(
        session.scalars(
            select(UsaliFinancialFact.business_date)
            .where(
                UsaliFinancialFact.property_id == property_id,
                UsaliFinancialFact.business_date >= start,
                UsaliFinancialFact.business_date <= end,
            )
            .distinct()
            .order_by(UsaliFinancialFact.business_date)
        )
    )


def push_month(
    session: Session, client: QboClient, *, property_id: str, month: str
) -> list[tuple[date, PushResult]]:
    """Push every fact date in the month; per-date outcomes are independent.

    A date whose JE cannot even be BUILT — facts lacking GL codes
    (`UnmappedGlError`) or GL codes absent from the chart of accounts
    (`ValueError`) — yields a `failed` outcome (no ledger row; nothing was
    attempted against QBO) without stopping the remaining dates. Transport
    errors (httpx) still propagate: an unreachable QBO fails every date the
    same way, so aborting there is more honest than N identical failures.
    """
    results: list[tuple[date, PushResult]] = []
    for d in fact_dates(session, property_id=property_id, month=month):
        try:
            results.append(
                (d, push_day(session, client, property_id=property_id, business_date=d))
            )
        except (UnmappedGlError, ValueError) as exc:
            results.append((d, PushResult(status="failed", qbo_je_id=None, message=str(exc)[:500])))
    return results


def push_ledger_rows(
    session: Session, *, property_id: str | None = None, month: str | None = None
) -> list[QboPushLedger]:
    """Push-ledger rows, optionally filtered, ordered by (property, date)."""
    query = select(QboPushLedger)
    if property_id is not None:
        query = query.where(QboPushLedger.property_id == property_id)
    if month is not None:
        start, end = month_bounds(month)
        query = query.where(
            QboPushLedger.business_date >= start, QboPushLedger.business_date <= end
        )
    query = query.order_by(QboPushLedger.property_id, QboPushLedger.business_date)
    return list(session.scalars(query))
