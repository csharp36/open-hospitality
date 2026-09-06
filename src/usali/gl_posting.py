"""The GL posting engine (ADR-011 §3; design D-OH27.5, .6, .7).

Sources are a closed registry (the checklist/CRM_PROVIDERS idiom): each
owns a build function returning a balanced JePlan or None (nothing to
post). post_and_record() is the single entry point every caller shares —
ingestion hook, timecard approval, CLI, API — so idempotency, the period
gate, and the failure ledger behave identically everywhere.

The pms_daily builder is `qbo_push.build_journal_entry`'s economics,
moved here in OH-27 Task 6 (`qbo_push.build_journal_entry` now delegates
back to it):

- Fact amounts carry the P&L sign convention (revenue/taxes positive,
  settlements/write-offs negative), so each GL group's NET sum picks its
  own side: positive nets are CREDITS, negative nets are DEBITS of the
  absolute value. Line amounts are therefore always positive — direction
  lives in `posting`, never in the sign (ck_journal_line_amount_positive
  is the DB wall for the same rule).
- Buckets, in line order: revenue facts (non-NULL USALI schedule) by GL,
  pass-through taxes by GL, settlements by GL, and everything else that
  is unscheduled (e.g. Non-Operating write-offs) by GL — the last bucket
  must be posted explicitly or the balancing remainder would silently
  overstate.
- The remainder (credits − debits so far) posts to the account carrying
  the `guest_ledger_clearing` system role — the revenue earned today but
  not yet settled. A negative remainder flips the balancing line to the
  credit side. Zero-net GL groups are omitted.

`request_hash` is a sha256 fingerprint of the plan's economic content
(property, date, and every (account, posting, amount) line) — the
canonical construction is unchanged by the move (see `_finish_plan`).
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali import fiscal
from usali.invariants import require_not_none
from usali.models import (
    GlAccount,
    GlPeriodEvent,
    GlPostingLedger,
    JournalEntry,
    JournalLine,
    UsaliFinancialFact,
    UsaliLaborFact,
)
from usali.reporting import SETTLEMENTS_MAJOR, TAXES_MAJOR

_QUANT_4DP = Decimal("0.0001")  # facts are Numeric(15,4); canonical hash scale

Posting = Literal["Debit", "Credit"]


class UnmappedGlError(Exception):
    """Facts in the posting scope carry no GL account code — the build refuses.

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


class ChartAccountMissingError(ValueError):
    """A fact's GL code has no active row in the chart of accounts.

    A ValueError subclass so pre-move callers catching ValueError from
    `qbo_push.build_journal_entry` (e.g. `push_month`) keep working; typed
    so `post_and_record` can turn it into a failed ledger row without
    swallowing `_finish_plan`'s out-of-balance ValueError, which must
    escape loudly."""


class SystemRoleMissingError(Exception):
    """The org's chart has no active account carrying the named role."""

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(
            f"the chart of accounts has no active account with system role "
            f"{role!r} — seed or edit the chart (usali gl-seed-chart)"
        )


class PeriodClosedError(Exception):
    """Posting dated inside a closed fiscal period (D-OH27.7)."""

    def __init__(self, property_id: str, period_key: str) -> None:
        self.period_key = period_key
        super().__init__(
            f"period {period_key} is closed for property {property_id}; "
            f"reopen it (with a reason) before posting into it"
        )


@dataclass(frozen=True)
class JeLine:
    gl_account_code: str
    account_name: str
    posting: Posting
    amount: Decimal  # always positive; direction is `posting`
    memo: str
    # Drill-through provenance: set only when a GL group has exactly one
    # fact; grouped lines carry None (facts stay reachable via the
    # (property, date, account→USALI) linkage).
    fact_id: int | None = None


@dataclass(frozen=True)
class JePlan:
    property_id: str
    business_date: date
    lines: list[JeLine]
    total_debits: Decimal
    total_credits: Decimal
    request_hash: str


def has_active_chart(session: "Session") -> bool:
    """THE definition of "GL is on" for an org: at least one active
    gl_account row visible to this session. Every caller asking the
    question must use this, not its own query — four copies had already
    grown by the time it was extracted."""
    return (
        session.scalar(
            select(GlAccount.account_code).where(GlAccount.is_active.is_(True)).limit(1)
        )
        is not None
    )


def chart(session: "Session") -> dict[str, GlAccount]:
    """Active chart rows by code, for whatever org the session can see
    (the ORM/RLS wall scopes an app session; owner sessions see all)."""
    rows = session.scalars(
        select(GlAccount).where(GlAccount.is_active.is_(True))
    ).all()
    return {row.account_code: row for row in rows}


def role_account(session: "Session", role: str) -> GlAccount:
    row = session.scalar(
        select(GlAccount).where(
            GlAccount.system_role == role, GlAccount.is_active.is_(True)
        )
    )
    if row is None:
        raise SystemRoleMissingError(role)
    return row


def _group_by_gl(facts: list[UsaliFinancialFact]) -> dict[str, Decimal]:
    """NET amount per GL account code (facts already validated as GL-mapped)."""
    sums: dict[str, Decimal] = {}
    for f in facts:
        code = require_not_none(
            f.gl_account_code,
            "journal-entry grouping received a fact without a GL account code",
        )
        sums[code] = sums.get(code, Decimal("0")) + Decimal(str(f.amount))
    return sums


def _finish_plan(property_id: str, business_date: date, lines: list[JeLine]) -> JePlan:
    """Totals + canonical hash, shared by every builder and by
    `plan_of_entry`'s reconstruction — one hash derivation, everywhere."""
    total_credits = sum(
        (line.amount for line in lines if line.posting == "Credit"), Decimal("0")
    )
    total_debits = sum(
        (line.amount for line in lines if line.posting == "Debit"), Decimal("0")
    )
    if total_debits != total_credits:
        raise ValueError(
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


def build_pms_daily_plan(
    session: "Session",
    property_id: str,
    business_date: date,
    *,
    account_names: dict[str, str] | None = None,
    balancing_account: str | None = None,
) -> JePlan | None:
    """Build the balanced JE plan for one property + business date.

    Returns None (rather than raising NoFactsError) when the day has no
    facts — "nothing to post" is a source outcome, not an error, in the
    engine. Raises `UnmappedGlError` when any fact in scope lacks a GL
    account code, `SystemRoleMissingError` when a balancing line is needed
    and no account carries `guest_ledger_clearing`, and
    `ChartAccountMissingError` when a fact's GL code is missing from the
    chart.

    `account_names` + `balancing_account` are the YAML-chart shim for
    `qbo_push.build_journal_entry`'s fact path: when given, names come from
    that mapping and the balancing line posts to `balancing_account` instead
    of the role lookup. This is the surviving fallback for orgs that have
    not seeded a DB chart (the QBO push loses nothing it had before OH-27);
    it retires only when the push requires a seeded chart, which no task in
    this plan does.
    """
    facts = session.scalars(
        select(UsaliFinancialFact).where(
            UsaliFinancialFact.property_id == property_id,
            UsaliFinancialFact.business_date == business_date,
        )
    ).all()
    if not facts:
        return None
    unmapped = sorted(
        {
            (f.usali_major_category, f.usali_sub_category, f.usali_line_item)
            for f in facts
            if f.gl_account_code is None
        }
    )
    if unmapped:
        raise UnmappedGlError(unmapped)

    names = (
        account_names
        if account_names is not None
        else {code: row.name for code, row in chart(session).items()}
    )

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
        by_code: dict[str, list[UsaliFinancialFact]] = {}
        for f in bucket:
            by_code.setdefault(str(f.gl_account_code), []).append(f)
        for gl, net in sorted(_group_by_gl(bucket).items()):
            if net == 0:
                continue  # a zero line adds nothing to either side; omitted
            if gl not in names:
                missing_accounts.add(gl)
                continue
            posting: Posting = "Credit" if net > 0 else "Debit"
            group = by_code[gl]
            lines.append(
                JeLine(
                    gl_account_code=gl,
                    account_name=names[gl],
                    posting=posting,
                    amount=abs(net),
                    memo=memo,
                    fact_id=group[0].fact_id if len(group) == 1 else None,
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
        if account_names is not None:
            # The YAML shim path: the balancing code is the caller's constant.
            bal_code, bal_name = balancing_account, (
                account_names.get(balancing_account) if balancing_account else None
            )
            if bal_code is None or bal_name is None:
                missing_accounts.add(bal_code or "<balancing>")
                bal_code = None
        else:
            bal = role_account(session, "guest_ledger_clearing")
            bal_code, bal_name = bal.account_code, bal.name
        if bal_code is not None:
            posting = "Debit" if remainder > 0 else "Credit"
            lines.append(
                JeLine(
                    gl_account_code=bal_code,
                    account_name=bal_name,
                    posting=posting,
                    amount=abs(remainder),
                    memo=f"Guest ledger clearing {day}",
                )
            )
    if missing_accounts:
        raise ChartAccountMissingError(
            f"GL account(s) {sorted(missing_accounts)} are not in the chart of "
            f"accounts — add them (gl_account rows; on the transitional YAML "
            f"path, qbo_accounts.yaml)"
        )

    return _finish_plan(property_id, business_date, lines)


def build_payroll_accrual_plan(
    session: "Session", property_id: str, business_date: date
) -> JePlan | None:
    """Debit labor_wages_expense per department (one line each, department
    named in the memo), credit accrued_payroll with the total. est_cost is
    the estimate labor.py wrote on timecard approval; truing to PayRun
    actuals is deliberately out of scope (design §9)."""
    rows = session.execute(
        select(UsaliLaborFact.department_id, UsaliLaborFact.est_cost).where(
            UsaliLaborFact.property_id == property_id,
            UsaliLaborFact.business_date == business_date,
        )
    ).all()
    if not rows:
        return None
    by_dept: dict[int | None, Decimal] = {}
    for dept, cost in rows:
        by_dept[dept] = by_dept.get(dept, Decimal("0")) + Decimal(str(cost))
    total = sum(by_dept.values(), Decimal("0"))
    if total == 0:
        # Nothing to post — decided BEFORE the role lookups, so a zero-net
        # labor day on a roleless chart is a skip, not a failed row.
        return None
    expense = role_account(session, "labor_wages_expense")
    liability = role_account(session, "accrued_payroll")
    # A negative department net (a correction outweighing the day's cost)
    # flips the line to the credit side — the same abs/side convention the
    # pms builder applies to negative GL nets. Line amounts stay positive;
    # direction lives in `posting`.
    lines = [
        JeLine(
            gl_account_code=expense.account_code,
            account_name=expense.name,
            posting="Debit" if cost > 0 else "Credit",
            amount=abs(cost),
            memo=f"Labor accrual dept {dept if dept is not None else 'unassigned'} "
                 f"{business_date.isoformat()}",
        )
        for dept, cost in sorted(
            by_dept.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)
        )
        if cost != 0
    ]
    lines.append(
        JeLine(
            gl_account_code=liability.account_code,
            account_name=liability.name,
            posting="Credit" if total > 0 else "Debit",
            amount=abs(total),
            memo=f"Accrued payroll {business_date.isoformat()}",
        )
    )
    return _finish_plan(property_id, business_date, lines)


@dataclass(frozen=True)
class PostingSource:
    source_type: str
    build_plan: Callable[["Session", str, date], JePlan | None]


POSTING_SOURCES: tuple[PostingSource, ...] = (
    PostingSource("pms_daily", build_pms_daily_plan),
    PostingSource("payroll_accrual", build_payroll_accrual_plan),
)

_SOURCES = {s.source_type: s for s in POSTING_SOURCES}

OutcomeStatus = Literal["posted", "noop", "reposted", "reversed", "skipped", "failed"]


@dataclass(frozen=True)
class PostOutcome:
    status: OutcomeStatus
    entry_id: int | None
    message: str | None


def period_state(session: "Session", property_id: str, period_key: str) -> str:
    """Return "closed" or "open": derived from the LAST GlPeriodEvent,
    never stored (D-OH27.7)."""
    last = session.scalar(
        select(GlPeriodEvent.event)
        .where(
            GlPeriodEvent.property_id == property_id,
            GlPeriodEvent.period_key == period_key,
        )
        .order_by(GlPeriodEvent.period_event_id.desc())
        .limit(1)
    )
    return "closed" if last == "close" else "open"


def _write_entry(
    session: "Session",
    plan: JePlan,
    *,
    source_type: str,
    actor: str,
    reversal_of: int | None = None,
    flip: bool = False,
) -> JournalEntry:
    entry = JournalEntry(
        property_id=plan.property_id,
        business_date=plan.business_date,
        source_type=source_type,
        source_hash=plan.request_hash,
        posted_by=actor,
        reversal_of=reversal_of,
        memo=f"reversal of entry {reversal_of}" if flip else None,
    )
    session.add(entry)
    session.flush()
    for line in plan.lines:
        posting = line.posting
        if flip:
            posting = "Debit" if posting == "Credit" else "Credit"
        session.add(
            JournalLine(
                entry_id=entry.entry_id,
                account_code=line.gl_account_code,
                posting=posting,
                amount=line.amount,
                memo=line.memo,
                fact_id=line.fact_id,
            )
        )
    session.flush()
    return entry


def plan_of_entry(session: "Session", entry: JournalEntry) -> JePlan:
    """Rebuild a JePlan from a posted entry's lines — the reversal input,
    and the QBO export input (`qbo_push.plan_for_push`'s journal path).
    The hash is re-derived through
    `_finish_plan` — the same tail every builder uses — from the stored
    lines (g1a0glcore's REVOKE is where line immutability is enforced)."""
    rows = session.scalars(
        select(JournalLine)
        .where(JournalLine.entry_id == entry.entry_id)
        .order_by(JournalLine.line_id)
    ).all()
    names = chart(session)
    lines = [
        JeLine(
            gl_account_code=row.account_code,
            account_name=(
                names[row.account_code].name
                if row.account_code in names
                else row.account_code
            ),
            posting=row.posting,  # type: ignore[arg-type]  # ck_journal_line_posting
            amount=Decimal(str(row.amount)),
            memo=row.memo or "",
            fact_id=row.fact_id,
        )
        for row in rows
    ]
    return _finish_plan(entry.property_id, entry.business_date, lines)


def post_and_record(
    session: "Session",
    *,
    property_id: str,
    business_date: date,
    source_type: str,
    actor: str,
) -> PostOutcome:
    """The one entry point. The named refusal types (`UnmappedGlError`,
    `SystemRoleMissingError`, `PeriodClosedError`,
    `ChartAccountMissingError`, `FiscalCalendarNotConfigured`) become
    failed ledger rows rather than exceptions — the ingestion hook commits
    in the same transaction as promotion, so a GL refusal must not
    quarantine a parsed file. What deliberately still escapes:
    `IntegrityError` from the ledger's unique constraint (the concurrency
    arbiter — see GlPostingLedger's docstring) and invariant violations
    such as `_finish_plan`'s out-of-balance ValueError.

    Outcomes: "posted" (first current entry for the grain), "noop" (standing entry,
    same source hash), "reposted" (facts changed — reversal plus a fresh
    entry), "reversed" (the facts are GONE but an entry stands — a reversal
    is written, the ledger row is deleted, and the grain returns to
    unposted; `entry_id` names the reversal entry), "skipped" (no chart, or
    no facts and no standing entry), "failed" (a refusal, recorded on the
    ledger row)."""
    if not has_active_chart(session):
        return PostOutcome("skipped", None, "no chart of accounts; GL is off")

    source = _SOURCES[source_type]
    now = datetime.now(UTC)
    row = session.scalar(
        select(GlPostingLedger).where(
            GlPostingLedger.property_id == property_id,
            GlPostingLedger.business_date == business_date,
            GlPostingLedger.source_type == source_type,
        )
    )

    def _fail(message: str) -> PostOutcome:
        nonlocal row
        if row is None:
            row = GlPostingLedger(
                property_id=property_id,
                business_date=business_date,
                source_type=source_type,
                status="failed",
            )
            session.add(row)
        if row.entry_id is None:
            row.status = "failed"
        row.message = message[:500]
        row.posted_at = now
        session.flush()
        return PostOutcome("failed", row.entry_id, row.message)

    try:
        cfg = fiscal.require_config(fiscal.config_for(session, property_id))
        period = fiscal.period_containing(cfg, business_date)
        plan = source.build_plan(session, property_id, business_date)
        if plan is None:
            if row is None or row.entry_id is None:
                return PostOutcome("skipped", None, None)
            # The grain emptied AFTER an entry was posted (a re-transform,
            # or a timecard reopen demoting the day's last facts). A
            # standing entry over no facts is a phantom amount: reverse it
            # and delete the ledger row — the grain is back to unposted.
            # (gl_posting_ledger keeps DELETE; g1a0glcore's _IMMUTABLE
            # revoke covers only the three journal tables.) In a closed
            # period the refusal lands on the ledger row like every other.
            if period_state(session, property_id, period) == "closed":
                return _fail(
                    f"facts for {property_id} {business_date.isoformat()} "
                    f"are gone but period {period} is closed; the standing "
                    "entry cannot be reversed"
                )
            prior = session.get(JournalEntry, row.entry_id)
            reversal = _write_entry(
                session,
                plan_of_entry(session, prior),
                source_type=source_type,
                actor=actor,
                reversal_of=prior.entry_id,
                flip=True,
            )
            session.delete(row)
            session.flush()
            return PostOutcome("reversed", reversal.entry_id, None)
        if row is not None and row.entry_id is not None:
            if row.source_hash == plan.request_hash:
                if row.message is not None:
                    row.message = None
                    session.flush()
                return PostOutcome("noop", row.entry_id, None)
        if period_state(session, property_id, period) == "closed":
            raise PeriodClosedError(property_id, period)
        if row is not None and row.entry_id is not None:
            prior = session.get(JournalEntry, row.entry_id)
            _write_entry(
                session,
                plan_of_entry(session, prior),
                source_type=source_type,
                actor=actor,
                reversal_of=prior.entry_id,
                flip=True,
            )
            entry = _write_entry(session, plan, source_type=source_type, actor=actor)
            row.entry_id, row.source_hash = entry.entry_id, plan.request_hash
            row.status, row.message, row.posted_at = "posted", None, now
            session.flush()
            return PostOutcome("reposted", entry.entry_id, None)
        entry = _write_entry(session, plan, source_type=source_type, actor=actor)
        if row is None:
            row = GlPostingLedger(
                property_id=property_id,
                business_date=business_date,
                source_type=source_type,
                status="posted",
            )
            session.add(row)
        row.entry_id, row.source_hash = entry.entry_id, plan.request_hash
        row.status, row.message, row.posted_at = "posted", None, now
        session.flush()
        return PostOutcome("posted", entry.entry_id, None)
    except (
        UnmappedGlError,
        SystemRoleMissingError,
        PeriodClosedError,
        ChartAccountMissingError,
        fiscal.FiscalCalendarNotConfigured,
    ) as exc:
        return _fail(str(exc))


def period_key_for(session: "Session", property_id: str, day: date) -> str:
    """The fiscal period key containing `day`, for use by callers (close/
    reopen, the API) that need it outside `post_and_record`'s own lookup."""
    cfg = fiscal.require_config(fiscal.config_for(session, property_id))
    return fiscal.period_containing(cfg, day)


@dataclass(frozen=True)
class CloseGaps:
    """The two directions a close can be out of sync with the facts
    (design D-OH27.7, extended): `unposted` names financial-fact dates in
    the period with no current posted `pms_daily` entry (nothing was
    posted, or the standing entry failed) — deliberately pms_daily-only,
    because "facts with no entry" means `usali_financial_fact` here; a
    labor day with no accrual is the ordinary no-chart/no-cost case, not a
    gap. `orphaned` covers BOTH sources: dates with a current entry whose
    fact side is empty — `usali_financial_fact` for `pms_daily`,
    `usali_labor_fact` for `payroll_accrual` — a grain emptied after
    posting and never re-posted (a re-post reverses the entry via
    `post_and_record`'s plan-is-None branch, which removes the date from
    both directions)."""

    unposted: list[date]
    orphaned: list[date]


def close_period(
    session: "Session", *, property_id: str, period_key: str, actor: str
) -> CloseGaps:
    """Append a close event; return the period's gaps in both directions
    (see `CloseGaps`), so closing over either kind of gap is a visible
    choice. Idempotent: closing an already-closed period is a no-op that
    still returns the gaps as they stand today."""
    cfg = fiscal.require_config(fiscal.config_for(session, property_id))
    start, end = fiscal.resolve_period(cfg, period_key)
    fact_dates = set(
        session.scalars(
            select(UsaliFinancialFact.business_date)
            .where(
                UsaliFinancialFact.property_id == property_id,
                UsaliFinancialFact.business_date >= start,
                UsaliFinancialFact.business_date <= end,
            )
            .distinct()
        )
    )
    labor_dates = set(
        session.scalars(
            select(UsaliLaborFact.business_date)
            .where(
                UsaliLaborFact.property_id == property_id,
                UsaliLaborFact.business_date >= start,
                UsaliLaborFact.business_date <= end,
            )
            .distinct()
        )
    )
    ledger_rows = session.scalars(
        select(GlPostingLedger).where(
            GlPostingLedger.property_id == property_id,
            GlPostingLedger.source_type.in_(("pms_daily", "payroll_accrual")),
            GlPostingLedger.business_date >= start,
            GlPostingLedger.business_date <= end,
        )
    ).all()
    posted_dates = {
        row.business_date
        for row in ledger_rows
        if row.source_type == "pms_daily" and row.status == "posted"
    }
    # The fact side an entry must still be justified by, per source
    # (see CloseGaps).
    fact_side = {"pms_daily": fact_dates, "payroll_accrual": labor_dates}
    gaps = CloseGaps(
        unposted=sorted(fact_dates - posted_dates),
        orphaned=sorted({
            row.business_date
            for row in ledger_rows
            if row.entry_id is not None
            and row.business_date not in fact_side[row.source_type]
        }),
    )
    if period_state(session, property_id, period_key) != "closed":
        session.add(
            GlPeriodEvent(
                property_id=property_id, period_key=period_key,
                event="close", actor_subject=actor,
            )
        )
        session.flush()
    return gaps


def reopen_period(
    session: "Session", *, property_id: str, period_key: str, actor: str, reason: str
) -> None:
    """Append a reopen event; requires a non-empty reason (the audit trail
    IS this table, per GlPeriodEvent's docstring). Idempotent: reopening an
    already-open period is a no-op, like the D-B4.5 PUT shape."""
    if not reason or not reason.strip():
        raise ValueError("reopening a closed period requires a reason")
    if period_state(session, property_id, period_key) != "closed":
        return
    session.add(
        GlPeriodEvent(
            property_id=property_id, period_key=period_key,
            event="reopen", actor_subject=actor, reason=reason.strip(),
        )
    )
    session.flush()
