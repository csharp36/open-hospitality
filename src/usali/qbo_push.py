"""Idempotent QBO push orchestration (P8 Task 4).

The journal-entry builder that used to live here moved to
`usali.gl_posting.build_pms_daily_plan` in OH-27 Task 6 (economics
unchanged — the bucketing, balancing, and canonical `request_hash` are
documented there). Since Task 10, `push_day` exports the POSTED JOURNAL
when the grain has a current `pms_daily` entry (`plan_for_push` below);
`build_journal_entry` — the fact path, with the `mapping/qbo_accounts.yaml`
chart when the org has no active `gl_account` rows — survives as the
fallback for orgs that have not seeded a chart (GL off loses nothing it
had). The fact path retires only when the push requires a seeded chart,
which no OH-27 task does. Its contract is unchanged: `NoFactsError` on an
empty day.

`request_hash`'s 50-char prefix doubles as Intuit's `requestid`
idempotency key, and `qbo_push_ledger` records it per (property,
business_date) so `push_day` can distinguish "already pushed this exact
JE" (no-op) from "the pushed JE no longer matches the current plan"
(mark stale, refuse — the posted JE needs manual correction first;
automated void/amend is out of scope for P8).

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
from usali.models import (
    GlPostingLedger,
    JournalEntry,
    QboPushLedger,
    UsaliFinancialFact,
)
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

    YAML fallback: when the org has no ACTIVE `gl_account` rows, account
    names come from `accounts_path` (the pre-OH27 YAML chart) and the
    balancing line posts to `_BALANCING_ACCOUNT` — pushes for orgs that
    never seeded a DB chart keep behaving exactly as before OH-27. This is
    the fact path's surviving role after Task 10 (`plan_for_push` prefers
    the posted journal); it retires only when the push requires a seeded
    chart, which no OH-27 task does. `gl_posting.has_active_chart` is the
    same "is GL on" question `gl_posting.post_and_record` asks.
    """
    if not gl_posting.has_active_chart(session):
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


def plan_for_push(session: Session, *, property_id: str, business_date: date) -> JePlan:
    """Select the plan `push_day` exports: the posted journal, facts as fallback.

    The current entry for the (property, date, pms_daily) grain is the one
    the `gl_posting_ledger` row names in `entry_id` — never a latest-entry
    heuristic, because after a reverse-and-repost the newest entry is the
    correction only BECAUSE the ledger row says so. With such a row (and an
    active chart), the plan is rebuilt from that entry's stored lines
    (`gl_posting.plan_of_entry`, which re-derives `request_hash` through the
    same `_finish_plan` tail every builder uses — so this ledger's
    idempotency and staleness comparisons are against the same hash space
    as before).

    The fact path (`build_journal_entry`) is the fallback when:

    - the org has no active chart — GL is off, and the push keeps behaving
      exactly as it did before OH-27; or
    - no ledger row names a current entry. That covers grains never posted
      AND reversed ones: a reversed grain has NO ledger row
      (`gl_posting.post_and_record`'s plan-is-None branch deletes it;
      `test_an_emptied_grain_reverses_its_entry` pins that), so it falls
      back here — and raises `NoFactsError` when the facts really are gone,
      the honest outcome, rather than pushing an entry the journal has
      already reversed.
    """
    if not gl_posting.has_active_chart(session):
        return build_journal_entry(
            session, property_id=property_id, business_date=business_date
        )
    entry_id = session.scalar(
        select(GlPostingLedger.entry_id).where(
            GlPostingLedger.property_id == property_id,
            GlPostingLedger.business_date == business_date,
            GlPostingLedger.source_type == "pms_daily",
            GlPostingLedger.entry_id.is_not(None),
        )
    )
    if entry_id is None:
        return build_journal_entry(
            session, property_id=property_id, business_date=business_date
        )
    return gl_posting.plan_of_entry(session, session.get(JournalEntry, entry_id))


def push_day(
    session: Session, client: QboClient, *, property_id: str, business_date: date
) -> PushResult:
    """Push one property+date JE idempotently, recording the outcome.

    The plan pushed is `plan_for_push`'s selection: the posted journal entry
    when the grain has a current one and the org an active chart, the facts
    otherwise. Only `pms_daily` entries are exported — `payroll_accrual`
    entries stay internal, so owners' QBO books get exactly what they got
    before OH-27.

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
    plan = plan_for_push(session, property_id=property_id, business_date=business_date)
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
            f"the pushed JE no longer matches the current plan: pushed hash "
            f"{row.request_hash[:12]} != current {plan.request_hash[:12]}; "
            f"correct the QBO JE manually"
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
