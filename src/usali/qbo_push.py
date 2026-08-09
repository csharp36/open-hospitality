"""Journal-entry builder and idempotent QBO push orchestration (P8 Task 4).

`build_journal_entry` turns one property's financial facts for one business
date into a balanced double-entry plan:

- Fact amounts carry the P&L sign convention (revenue/taxes positive,
  settlements/write-offs negative), so each GL group's NET sum picks its own
  side: positive nets are CREDITS, negative nets are DEBITS of the absolute
  value. Line amounts are therefore always positive — direction lives in
  PostingType, never in the sign (real QBO rejects non-positive amounts).
- Buckets, in line order: revenue facts (non-NULL USALI schedule) by GL,
  pass-through taxes by GL, settlements by GL, and everything else that is
  unscheduled (e.g. Non-Operating write-offs) by GL — the last bucket must be
  posted explicitly or the balancing remainder would silently overstate.
- The remainder (credits − debits so far) posts to account 1210 "Guest Ledger
  Clearing" — the revenue earned today but not yet settled. A negative
  remainder flips the balancing line to the credit side. Zero-net GL groups
  are omitted (the mock, like real QBO, rejects zero amounts).

`request_hash` is a sha256 fingerprint of the plan's economic content
(property, date, and every (account, posting, amount) line). Its 50-char
prefix doubles as Intuit's `requestid` idempotency key, and `qbo_push_ledger`
records it per (property, business_date) so `push_day` can distinguish
"already pushed this exact JE" (no-op) from "facts changed after pushing"
(mark stale, refuse — the posted JE needs manual correction first; automated
void/amend is out of scope for P8).

Writes: `push_day` commits the session itself — each date's ledger outcome
must survive later dates failing (`push_month` outcomes are independent).
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import QboPushLedger, UsaliFinancialFact
from usali.qbo_client import QboClient, QboError
# month_bounds lives in usali.reporting (the CPA pack shares it); re-exported here
# so existing `qbo_push.month_bounds` callers keep working.
from usali.reporting import SETTLEMENTS_MAJOR, TAXES_MAJOR, NoFactsError, month_bounds

_DEFAULT_ACCOUNTS = Path(__file__).resolve().parents[2] / "mapping" / "qbo_accounts.yaml"
# The JE balancing account (see the mapping/qbo_accounts.yaml header). Builder-only:
# no dictionary entry maps to it — it absorbs each day's un-settled remainder.
_BALANCING_ACCOUNT = "1210"
_REQUESTID_MAX = 50  # Intuit's requestid length cap
_QUANT_4DP = Decimal("0.0001")  # facts are Numeric(15,4); canonical hash scale

Posting = Literal["Debit", "Credit"]
PushStatus = Literal["pushed", "already-pushed", "stale", "failed"]


class UnmappedGlError(Exception):
    """Facts in the push scope carry no GL account code — the push refuses.

    `lines` holds the distinct (major, sub_category, line_item) triples that
    need curation in the mapping YAML (then re-seed + re-transform).
    """

    def __init__(self, lines: list[tuple[str, str, str]]) -> None:
        self.lines = lines
        listed = "; ".join(f"{major} / {sub} / {item}" for major, sub, item in lines)
        super().__init__(
            f"{len(lines)} fact line(s) have no GL account code "
            f"(curate gl_account_code in the mapping YAML, re-seed, re-transform): {listed}"
        )


@dataclass(frozen=True)
class JeLine:
    gl_account_code: str
    account_name: str
    posting: Posting
    amount: Decimal  # always positive; direction is `posting`
    memo: str


@dataclass(frozen=True)
class JePlan:
    property_id: str
    business_date: date
    lines: list[JeLine]
    total_debits: Decimal
    total_credits: Decimal
    request_hash: str


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


def _group_by_gl(facts: list[UsaliFinancialFact]) -> dict[str, Decimal]:
    """NET amount per GL account code (facts already validated as GL-mapped)."""
    sums: dict[str, Decimal] = {}
    for f in facts:
        assert f.gl_account_code is not None  # guarded by the UnmappedGlError check
        sums[f.gl_account_code] = sums.get(f.gl_account_code, Decimal("0")) + Decimal(
            str(f.amount)
        )
    return sums


def build_journal_entry(
    session: Session,
    *,
    property_id: str,
    business_date: date,
    accounts_path: Path = _DEFAULT_ACCOUNTS,
) -> JePlan:
    """Build the balanced JE plan for one property + business date.

    Raises `NoFactsError` when the day has no facts, `UnmappedGlError` when any
    fact in scope lacks a GL account code, and `ValueError` when a fact's GL
    code (or the balancing account) is missing from the chart of accounts.
    """
    facts = (
        session.execute(
            select(UsaliFinancialFact).where(
                UsaliFinancialFact.property_id == property_id,
                UsaliFinancialFact.business_date == business_date,
            )
        )
        .scalars()
        .all()
    )
    if not facts:
        raise NoFactsError(
            f"no financial facts for property {property_id} on {business_date}"
        )
    unmapped = sorted(
        {
            (f.usali_major_category, f.usali_sub_category, f.usali_line_item)
            for f in facts
            if f.gl_account_code is None
        }
    )
    if unmapped:
        raise UnmappedGlError(unmapped)

    names = _load_account_names(accounts_path)

    revenue = [f for f in facts if f.usali_schedule_id is not None]
    unscheduled = [f for f in facts if f.usali_schedule_id is None]
    taxes = [f for f in unscheduled if f.usali_major_category == TAXES_MAJOR]
    settlements = [f for f in unscheduled if f.usali_major_category == SETTLEMENTS_MAJOR]
    # Everything else unscheduled (e.g. Non-Operating write-offs): posted to its
    # own GL, NOT silently dropped — dropping would keep the JE balanced but
    # overstate the balancing-line remainder.
    non_operating = [
        f
        for f in unscheduled
        if f.usali_major_category not in (TAXES_MAJOR, SETTLEMENTS_MAJOR)
    ]

    lines: list[JeLine] = []
    missing_accounts: set[str] = set()

    def _emit(bucket: list[UsaliFinancialFact], memo: str) -> None:
        for gl, net in sorted(_group_by_gl(bucket).items()):
            if net == 0:
                continue  # real QBO (and the mock) reject zero amounts
            if gl not in names:
                missing_accounts.add(gl)
                continue
            posting: Posting = "Credit" if net > 0 else "Debit"
            lines.append(
                JeLine(
                    gl_account_code=gl,
                    account_name=names[gl],
                    posting=posting,
                    amount=abs(net),
                    memo=memo,
                )
            )

    day = business_date.isoformat()
    _emit(revenue, f"Daily revenue {day}")
    _emit(taxes, f"Pass-through taxes {day}")
    _emit(settlements, f"Settlements {day}")
    _emit(non_operating, f"Non-operating {day}")

    credits = sum((line.amount for line in lines if line.posting == "Credit"), Decimal("0"))
    debits = sum((line.amount for line in lines if line.posting == "Debit"), Decimal("0"))
    remainder = credits - debits
    if remainder != 0:
        if _BALANCING_ACCOUNT not in names:
            missing_accounts.add(_BALANCING_ACCOUNT)
        else:
            posting: Posting = "Debit" if remainder > 0 else "Credit"
            lines.append(
                JeLine(
                    gl_account_code=_BALANCING_ACCOUNT,
                    account_name=names[_BALANCING_ACCOUNT],
                    posting=posting,
                    amount=abs(remainder),
                    memo=f"Guest ledger clearing {day}",
                )
            )
    if missing_accounts:
        raise ValueError(
            f"GL account(s) {sorted(missing_accounts)} are not in the chart of accounts "
            f"{accounts_path} — align the mapping YAMLs and qbo_accounts.yaml"
        )

    total_credits = sum(
        (line.amount for line in lines if line.posting == "Credit"), Decimal("0")
    )
    total_debits = sum((line.amount for line in lines if line.posting == "Debit"), Decimal("0"))
    if total_debits != total_credits:
        raise ValueError(  # pragma: no cover - the balancing line makes this unreachable
            f"journal entry out of balance for {property_id} {business_date}: "
            f"debits {total_debits} != credits {total_credits}"
        )

    canonical = "\n".join(
        [f"{property_id}|{business_date.isoformat()}"]
        + [
            f"{account}|{posting}|{amount}"
            for account, posting, amount in sorted(
                (line.gl_account_code, line.posting, str(line.amount.quantize(_QUANT_4DP)))
                for line in lines
            )
        ]
    )
    return JePlan(
        property_id=property_id,
        business_date=business_date,
        lines=lines,
        total_debits=total_debits,
        total_credits=total_credits,
        request_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


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
