"""Pay-run assembly, preflight, and provider round-trip (Pillar C2).

Assembly reuses B2's hours engine and B3's overtime engine on APPROVED
timecards. Preflight runs BEFORE any network call and names every blocker
(missing rate, missing sealed PII, unapproved cards, no pay schedule) so a
Payroll Admin fixes data problems without a failed provider submit.

SECURITY: plaintext PII exists ONLY inside `sync_employees`, opened from the
vault at the moment of send. It is never stored, logged, or included in an
error message. Preflight checks PRESENCE of sealed fields, never opens.
"""

import hashlib
import logging
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from usali.models import (
    DepositAccount,
    EXCLUDE_FROM_PAYROLL,
    AuditEvent,
    Employee,
    EmployeePayrollProfile,
    KioskDevice,
    PayRun,
    Punch,
    PayRunLine,
    PayRunLineProperty,
    PaySchedule,
    Property,
    ProviderEmployeeRef,
    SickLeaveLedger,
    Timecard,
    UsaliActualLaborFact,
    UsaliLaborFact,
    WageSettlement,
)
from usali.apportion import apportion
from usali.assignments import (
    AmbiguousAssignmentError,
    assignment_at,
    MixedExemptionError,
    employee_ids_paid_by_during,
    primary_assignment_on,
    resolve_exemption,
)
from usali.attribution import property_hours_for_timecard
from usali.deposit_accounts import account_slot, deposit_problems, routing_slot
from usali.labor import department_at
from usali.opener import Opener
from usali.overtime import compute_overtime
from usali.overtime_rules import rules_for
from usali.payroll_provider import (
    PlainDepositAccount,
    PayrollEmployee,
    PayrollProvider,
    PayRunEntry,
    ProviderCapabilities,
    ProviderError,
)
from usali.pii_crypto import SealedEnvelope
from usali.rates import RateError, rate_on
from usali.sick_leave import (
    SickDayTaken,
    balance_on,
    day_length,
    sick_days_taken,
)
from usali.timecards import compute_timecard, period_for

_LOG = logging.getLogger(__name__)

_CENTS = Decimal("0.01")
# The deposit destination left the profile in E5 — its completeness is
# `deposit_problems`' question now, checked beside this in preflight.
_REQUIRED_SEALED = ("ssn_sealed",)


@dataclass(frozen=True)
class PreflightEntry:
    employee_id: int
    regular_hours: Decimal
    ot_hours: Decimal
    dt_hours: Decimal
    hourly_rate: Decimal
    # G3: sick hours ride the same entry at the same single rate (1.0x —
    # the provider applies no multiplier). docs/reference/ca-sick-leave.md
    # §4a is the statute check that makes base-hourly the lawful figure on
    # any run this port accepts.
    sick_hours: Decimal = Decimal("0")
    # G5: the §246(i) display figure — the ledger fold as of the CHECK
    # DATE (the statute pins the notice to "the designated pay date with
    # the employee's payment of wages", ca-sick-leave.md §4b). None means
    # "do not display": the provider never declared it can render it.
    sick_balance_hours: Decimal | None = None


@dataclass
class PreflightReport:
    property_id: str
    period_start: date
    period_end: date
    check_date: date | None
    entries: list[PreflightEntry] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and bool(self.entries)


def _resolve_employee_sick(
    days: list[SickDayTaken], property_id: str, who: str
) -> tuple[Decimal, Decimal | None, list[str]]:
    """One employee's sick leave for this run: (hours to pay HERE, their one
    rate, problems). Days attributed to another property are another run's
    to pay and pass through silently; unattributable and unpriced days are
    NAMED problems — hours someone is owed money for must never vanish with
    nothing naming the employee. More than one rate across the sick days is
    refused like `_distinct_rates` refuses worked days: the port carries a
    single rate, and averaging or picking would submit money nobody
    authorised."""
    problems: list[str] = []
    unattributable = sorted(t.day for t in days if t.property_id is None)
    if unattributable:
        problems.append(
            f"{who}: sick leave taken "
            f"{', '.join(d.isoformat() for d in unattributable)} has no "
            "resolvable primary placement on the day taken, so it belongs "
            "to no property's pay run. Repair the placement or void the "
            "entry."
        )
    here = [t for t in days if t.property_id == property_id]
    unpriced = sorted(t.day for t in here if t.rate is None)
    if unpriced:
        problems.append(
            f"{who}: sick leave taken "
            f"{', '.join(d.isoformat() for d in unpriced)} with no pay rate "
            "in force on those dates. Submitting would pay those sick hours "
            "at another day's rate. Correct the rate's effective dates."
        )
    rates = {t.rate for t in here if t.rate is not None}
    if len(rates) > 1:
        problems.append(
            f"{who}: {len(rates)} different pay rates are in force across "
            "the sick days taken this period, and a pay run submits one "
            "rate per employee. Refusing to average or to pick. Split the "
            "period at the rate change, or correct the rates."
        )
    total = sum((t.hours for t in here), Decimal("0"))
    rate = rates.pop() if len(rates) == 1 else None
    return total, rate, problems


def _person_problems(
    session: Session, employee: Employee, who: str,
    provider_capabilities: ProviderCapabilities | None,
) -> list[str]:
    """Profile, chain, and staleness problems for one payable person —
    reported TOGETHER (the E5 serial-discovery finding), shared by the
    timecard path and the sick-only path (G3)."""
    profile = session.execute(
        select(EmployeePayrollProfile).where(
            EmployeePayrollProfile.employee_id == employee.employee_id
        )
    ).scalar_one_or_none()
    missing = [
        f.removesuffix("_sealed")
        for f in _REQUIRED_SEALED
        if profile is None or getattr(profile, f) is None
    ]
    person_problems = []
    if missing:
        person_problems.append(
            f"{who}: payroll profile missing {', '.join(missing)}"
        )
    person_problems.extend(deposit_problems(session, employee.employee_id, who))
    # G2: an update-capable provider heals staleness by re-sync inside
    # sync_employees, so no blocker. None (standalone preflight, unknown
    # provider) stays conservative — refuse, exactly as before.
    if (provider_capabilities is None
            or not provider_capabilities.supports_employee_update):
        person_problems.extend(
            _stale_provider_payload_problems(
                session, employee.employee_id, who
            )
        )
    return person_problems


def _carve_problem(
    session: Session, employee_id: int, who: str, est_gross: Decimal
) -> str | None:
    """Over-carved chain guard: the server cannot know NET at chain-write
    time, but HERE the estimated GROSS is known — and fixed amounts
    exceeding even gross certainly exceed net, so the remainder account
    would be starved and the provider's behaviour (reject? partial
    deposit? negative remainder?) is undefined in our port. Nobody
    models that failure, so it must not reach the provider — a named
    blocker instead (the E5 review's finding: this case was nobody's
    named problem). Amounts within gross may still exceed net (taxes);
    that narrower case remains the provider's arithmetic."""
    fixed_carve = session.execute(
        select(func.coalesce(func.sum(DepositAccount.allocation_value), 0))
        .where(
            DepositAccount.employee_id == employee_id,
            DepositAccount.allocation_type == "amount",
        )
    ).scalar_one()
    if Decimal(str(fixed_carve or 0)) > est_gross:
        return (
            f"{who}: fixed deposit amounts exceed this period's estimated "
            "gross pay -- the remainder account would be starved and the "
            "provider's handling is undefined. Reduce the fixed amounts "
            "or pay this period outside the integration."
        )
    return None


_EIGHT = Decimal("8")


def _stacked_sick_problems(
    session: Session, employee_id: int, who: str,
    day_hours: dict[date, Decimal], here_days: list[SickDayTaken],
) -> list[str]:
    """G7 (money C): sick usage dated on a day also WORKED stacks with the
    worked hours — correct for a partial day (worked 4h, went home sick
    4h), and almost surely a MISDATED entry when the stack exceeds the
    employee's day: 8h worked + 8h sick on one date is 16 paid hours,
    double money moving silently. The bound is the employee's own day
    length (trailing worked-day average, floored at 8; 8 with no history)
    — a genuine partial split fits inside it, a full-day double never
    does. Resolved per day taken, not sampled once."""
    problems: list[str] = []
    for t in sorted(here_days, key=lambda t: t.day):
        worked = day_hours.get(t.day, Decimal("0"))
        if worked <= 0:
            continue
        bound = day_length(session, employee_id, t.day) or _EIGHT
        if worked + t.hours > bound:
            problems.append(
                f"{who}: {t.day.isoformat()} carries {worked}h worked AND "
                f"{t.hours}h sick — {worked + t.hours}h against a {bound}h "
                "day. A full sick day dated on a worked day is almost "
                "surely misdated; correct the sick entry (or the punches) "
                "so only the unworked portion is sick."
            )
    return problems


def _late_sick_problems(
    session: Session, property_id: str, period_start: date,
) -> list[str]:
    """§246(n): sick leave is paid no later than the payday for the next
    period after it was taken. H6 (decision 7): CONTENT-BASED — for every
    earlier period this property already submitted, compare what the
    ledger NOW derives (this property's days, per employee) against what
    that run's lines STORED as paid (H5):

        derived > stored -> blocker: hours the run provably did not pay
                            (recorded too late, or a person the run never
                            carried) — silence leaves them unpaid forever;
        derived < stored -> paid-then-voided: money already moved and the
                            books changed under it — server-side log,
                            never a permanent preflight line (E3 lesson);
        equal            -> paid; silence.

    NO CLOCKS. The old guard compared ledger created_at against run
    submitted_at, which carried the whole timestamp-race family:
    transaction-start visibility, app-vs-DB skew, and the submit-window
    residual (an entry recorded during the provider call itself read as
    pre-submit and was silently never paid). "Does the ledger match what
    was paid" has no such windows — an entry landing at ANY moment after
    the run's derivation shows up as derived != stored.

    The population stays the G7 Critical's fix: derived comes from the
    LEDGER, stored from the run's LINES — never any current period's
    `paid_here`, so a terminated employee still surfaces. Whose problem a
    day is stays the per-day attribution (SickDayTaken.property_id, the
    primary in force that day). A period that never had a SUBMITTED run
    is the operator-era posture and raises nothing — a failed run paid
    nothing and has no lines to compare against.

    I4 (decision 4): "moved" is not "missing". A retroactive primary
    transfer across a paid period re-routes the attribution, making this
    branch read property A as paid-then-voided (false) and property B as
    unpaid — with the pay-outside instruction, the double-pay direction.
    So before branching on a mismatch, reconcile the employee's
    PERIOD-WIDE totals: derived sick summed across ALL properties (the
    attribution axis removed) vs stored sick summed across that period's
    SUBMITTED runs at every property. Equal → the hours were paid, only
    the attribution moved: a server-side log on both sides, never a
    blocker. Unequal — including a PARTIAL move — → the branches exactly
    as today: real unpaid hours still block by name. Same-period only,
    and the period is (start, end), not start alone: another period's —
    or another window's — paid sick is settled history, never a credit
    against this one.

    A usage_void in the window DISABLES the reconciliation for that
    employee (the I6 money lens): a pure attribution move never involves
    a void, but totals with a void in them can offset a clawback against
    a genuinely unpaid NEW day — voided 8h on the paid side plus a
    never-paid late 8h on the other side reads as "moved" by arithmetic
    while nothing moved at all. With a void present the branches run
    exactly as before I4: the new day blocks, the void logs. The
    reconciliation is also HOURS-only — it cannot see a rate difference
    across the move (recorded in the backlog); its log says so."""
    runs = session.execute(
        select(PayRun).where(
            PayRun.property_id == property_id,
            PayRun.period_start < period_start,
            PayRun.submitted_at.is_not(None),
        ).order_by(PayRun.period_start)
    ).scalars().all()
    problems: list[str] = []
    for run in runs:
        derived: dict[int, list[SickDayTaken]] = {}
        period_derived: dict[int, Decimal] = {}
        for t in sick_days_taken(session, run.period_start, run.period_end):
            period_derived[t.employee_id] = (
                period_derived.get(t.employee_id, Decimal("0")) + t.hours
            )
            if t.property_id == property_id:
                derived.setdefault(t.employee_id, []).append(t)
        stored = {
            employee_id: Decimal(str(sick_hours))
            for employee_id, sick_hours in session.execute(
                select(PayRunLine.employee_id, PayRunLine.sick_hours).where(
                    PayRunLine.pay_run_id == run.pay_run_id,
                )
            )
        }
        people = set(derived) | {e for e, h in stored.items() if h > 0}
        period_paid: dict[int, Decimal] | None = None
        voided: set[int] | None = None
        for employee_id in sorted(people):
            taken = derived.get(employee_id, [])
            derived_hours = sum(
                (t.hours for t in taken), Decimal("0")
            ).quantize(_CENTS)
            paid = stored.get(employee_id, Decimal("0"))
            if derived_hours == paid:
                continue
            # "Moved" vs "missing" (I4): the period-wide totals decide.
            # PAID sums SUBMITTED runs only — a draft or failed run paid
            # nothing, whatever rows hang off it — and only runs covering
            # EXACTLY this window (start AND end). A void in the window
            # disqualifies the employee from reconciliation entirely: the
            # totals can no longer certify a pure move (I6).
            if period_paid is None:
                period_paid = {
                    emp: Decimal(str(total))
                    for emp, total in session.execute(
                        select(
                            PayRunLine.employee_id,
                            func.sum(PayRunLine.sick_hours),
                        )
                        .join(PayRun,
                              PayRun.pay_run_id == PayRunLine.pay_run_id)
                        .where(
                            PayRun.period_start == run.period_start,
                            PayRun.period_end == run.period_end,
                            PayRun.submitted_at.is_not(None),
                        )
                        .group_by(PayRunLine.employee_id)
                    )
                }
            if voided is None:
                voided = set(session.execute(
                    select(SickLeaveLedger.employee_id).where(
                        SickLeaveLedger.entry_type == "usage_void",
                        SickLeaveLedger.effective_on >= run.period_start,
                        SickLeaveLedger.effective_on <= run.period_end,
                    ).distinct()
                ).scalars())
            whole_derived = period_derived.get(
                employee_id, Decimal("0")
            ).quantize(_CENTS)
            whole_paid = period_paid.get(
                employee_id, Decimal("0")
            ).quantize(_CENTS)
            if employee_id not in voided and whole_derived == whole_paid:
                _LOG.warning(
                    "pay run %s %s: employee_id=%s's sick hours for the %s "
                    "period moved between properties — this property now "
                    "derives %sh where its run paid %sh, but the period-wide "
                    "totals reconcile (%sh taken == %sh paid across that "
                    "period's submitted runs). Books reconciliation between "
                    "properties; no additional hours are owed — if the sick "
                    "rate in force differs across the move, reconcile the "
                    "pay difference deliberately.",
                    property_id, period_start.isoformat(), employee_id,
                    run.period_start.isoformat(), derived_hours, paid,
                    whole_derived, whole_paid,
                )
                continue
            employee = session.get(Employee, employee_id)
            assert employee is not None
            who = f"employee {employee_id} ({employee.full_name})"
            if derived_hours > paid:
                days = ", ".join(t.day.isoformat() for t in taken)
                problems.append(
                    f"{who}: sick leave taken {days} — the ledger shows "
                    f"{derived_hours}h at this property for the "
                    f"{run.period_start.isoformat()} period, but that "
                    f"period's pay run paid {paid}h. The difference was "
                    "recorded after the run was already submitted (or the "
                    "run never carried this person), so that run cannot "
                    "pay it and this run's window will not. Void the "
                    "entry and re-enter it dated in an open period, or "
                    "pay it outside the integration and void it."
                )
            else:
                _LOG.warning(
                    "pay run %s %s: the ledger now shows %sh of sick "
                    "leave for employee_id=%s in the %s period, but that "
                    "run paid %sh — voided after the money had already "
                    "moved; the books now say it was never taken. "
                    "Reconcile with the provider deliberately.",
                    property_id, period_start.isoformat(), derived_hours,
                    employee_id, run.period_start.isoformat(), paid,
                )
    return problems


def _late_worked_problems(
    session: Session, property_id: str, period_end: date,
) -> list[str]:
    """H4 (decision 4): the §246(n) treatment, applied to worked minutes.

    An UNLINKED punch is H1's marker: it landed in a period whose card
    was already approved, so its minutes are in nobody's card and
    nobody's pay — and nothing else in the system will ever pay them.
    Named here by employee and date, one line per day, until resolved
    (reopen the card and re-approve — H3 relinks it, and from then on
    `_paid_worked_problems` carries the comparison — or pay outside the
    integration if that period's run already submitted).

    The population is the PUNCHES themselves, never `paid_here` (the G7
    Critical's lesson verbatim): a terminated employee is in no period's
    population, and termination is exactly when a late correction lands
    after payroll ran. Attribution (revised by the H9 review): the
    PER-DAY PRIMARY placement — the same attribution the sick guard has
    always used — because reopen authority and the paycheck both live
    with the primary property; blocking the kiosk's property handed the
    blocker to an operator who could neither reopen the card nor pay
    the minutes, while the paying run submitted clean. The kiosk's
    property is the FALLBACK when no primary resolves that day: someone
    must see it, and that property recorded the work.

    Scoped to this run's period and earlier (`business_date <=
    period_end`): a later period's orphan is named by the run that
    should pay it — every orphan is some run's problem, and no orphan is
    every run's problem. No clock anywhere: the punch's linkage and date
    are the only facts consulted."""
    orphans = session.execute(
        select(Punch, KioskDevice.property_id)
        .join(KioskDevice, KioskDevice.device_id == Punch.kiosk_device_id)
        .where(
            Punch.timecard_id.is_(None),
            Punch.business_date <= period_end,
        )
    ).all()
    days: set[tuple[date, int]] = set()
    for punch, device_property in orphans:
        try:
            primary = primary_assignment_on(
                session, punch.employee_id, punch.business_date
            )
        except AmbiguousAssignmentError:
            primary = None
        owner = primary.property_id if primary is not None else device_property
        if owner == property_id:
            days.add((punch.business_date, punch.employee_id))
    problems: list[str] = []
    for day, employee_id in sorted(days):
        employee = session.get(Employee, employee_id)
        assert employee is not None
        who = f"employee {employee_id} ({employee.full_name})"
        problems.append(
            f"{who}: worked time punched on {day.isoformat()} is linked "
            "to no timecard — it landed after its period's card was "
            "approved, so it is in nobody's card and nobody's pay. "
            "Reopen that period's timecard and re-approve to bring the "
            "minutes in, or pay them outside the integration if that "
            "period's run already submitted."
        )
    return problems


def _paid_worked_problems(
    session: Session, property_id: str, period_start: date,
) -> list[str]:
    """H9 (the review's Critical): the worked-hours twin of the
    content-based §246(n) guard. H4 names an orphan punch only while it
    stays UNLINKED — and reopen (H3) RELINKS it, so the sanctioned
    resolution retired the only guard naming the minutes: silently
    unpaid, before any re-approval and forever after. Same species as
    every Pillar H fix: compare facts, not markers.

    For every earlier period this property submitted, per paid line,
    the card's CURRENT worked-hours derivation vs what the run paid as
    worked (stored `hours` minus stored `sick_hours` — H6 owns the sick
    side, so the two guards never double-name one delta):

        card not approved -> blocker: reopened and never re-approved —
                             its estimated facts were demoted and the
                             books are short with nothing else saying so;
        derived > stored  -> blocker: minutes the run provably did not
                             pay (a relinked late punch);
        derived < stored  -> log: the card now claims less than was paid
                             (a reopen-era correction) — money already
                             moved; reconcile deliberately;
        equal             -> paid; silence.

    The settlement act (I3) is the terminal resolution: the guard
    subtracts the settled sum, so blocker iff derived > stored +
    settled — and a post-settlement punch re-blocks with the residual
    only. Loudly wrong beats silently unpaid."""
    runs = session.execute(
        select(PayRun).where(
            PayRun.property_id == property_id,
            PayRun.period_start < period_start,
            PayRun.submitted_at.is_not(None),
        ).order_by(PayRun.period_start)
    ).scalars().all()
    problems: list[str] = []
    for run in runs:
        lines = session.execute(
            select(PayRunLine).where(PayRunLine.pay_run_id == run.pay_run_id)
        ).scalars().all()
        for line in sorted(lines, key=lambda li: li.employee_id):
            state, derived, stored_worked, settled = _worked_hours_facts(
                session, run, line
            )
            if state == "none" and stored_worked == 0:
                # The sick-only entrant (G3) has no card and worked 0 —
                # nothing to compare. (Worked hours with no card at all
                # would be a data defect; the comparison below names it
                # against derived 0.)
                continue
            if state == "unapproved":
                employee = session.get(Employee, line.employee_id)
                assert employee is not None
                problems.append(
                    f"employee {line.employee_id} ({employee.full_name}): "
                    f"the timecard for the {run.period_start.isoformat()} "
                    "period was reopened and never re-approved — its "
                    "estimated labor facts are demoted and the books are "
                    "short until someone re-approves it."
                )
                continue
            paid = (stored_worked + settled).quantize(_CENTS)
            if derived == paid:
                continue
            employee = session.get(Employee, line.employee_id)
            assert employee is not None
            who = f"employee {line.employee_id} ({employee.full_name})"
            if derived > paid:
                settled_clause = (
                    f" and {settled}h more was settled outside the "
                    f"integration — {(derived - paid).quantize(_CENTS)}h "
                    "remain unpaid"
                    if settled > 0 else ""
                )
                problems.append(
                    f"{who}: the timecard for the "
                    f"{run.period_start.isoformat()} period now shows "
                    f"{derived}h worked, but that period's pay run paid "
                    f"{stored_worked}h of worked time{settled_clause}. "
                    "The extra minutes landed after the run was already "
                    "submitted (a relinked late punch), so that run "
                    "cannot pay them and this run's window will not. Pay "
                    "them outside the integration and record the "
                    "settlement, or reconcile deliberately."
                )
            else:
                _LOG.warning(
                    "pay run %s %s: employee_id=%s's timecard for the %s "
                    "period now shows %sh worked but that run paid %sh "
                    "(%sh settled outside) — reduced after payment; "
                    "money already moved, reconcile with the provider "
                    "deliberately.",
                    property_id, period_start.isoformat(),
                    line.employee_id, run.period_start.isoformat(),
                    derived, stored_worked, settled,
                )
    return problems


def _worked_hours_facts(
    session: Session, run: PayRun, line: PayRunLine,
) -> tuple[str, Decimal, Decimal, Decimal]:
    """The ONE derivation of a paid line's worked-hours state — read by
    the preflight guard and the settlement act, so what the guard blocks
    on and what a settlement clears can never drift apart.

    Returns `(card_state, derived, stored_worked, settled)`:
    card_state in {"none", "unapproved", "approved"}; `derived` is the
    card's current worked derivation (0 with no approved card) under the
    same per-day cents quantization the promote/assembly path uses, so a
    settled period compares EXACTLY equal; `stored_worked` is what the
    run paid as worked (stored hours minus stored sick — H6 owns the
    sick side); `settled` is the sum of every recorded settlement for
    this (run, employee)."""
    stored_worked = (
        Decimal(str(line.hours)) - Decimal(str(line.sick_hours))
    ).quantize(_CENTS)
    settled = Decimal(str(session.execute(
        select(func.coalesce(func.sum(WageSettlement.hours), 0)).where(
            WageSettlement.pay_run_id == run.pay_run_id,
            WageSettlement.employee_id == line.employee_id,
        )
    ).scalar_one())).quantize(_CENTS)
    card = session.execute(
        select(Timecard).where(
            Timecard.employee_id == line.employee_id,
            Timecard.period_start == run.period_start,
        )
    ).scalar_one_or_none()
    if card is None:
        return "none", Decimal("0.00"), stored_worked, settled
    if card.status != "approved":
        return "unapproved", Decimal("0.00"), stored_worked, settled
    derived = sum(
        ((Decimal(d.worked_minutes) / Decimal("60")).quantize(_CENTS)
         for d in compute_timecard(session, card)),
        Decimal("0"),
    ).quantize(_CENTS)
    return "approved", derived, stored_worked, settled


class SettlementRefused(ValueError):
    """The settlement act cannot be recorded against the run's current
    state (409). The message names the resolution, never a value the
    caller could not already read."""


class SettlementNotFound(LookupError):
    """The run has no line for that employee (404). A dedicated class —
    not a bare LookupError — so the endpoint's 404 detail can only ever
    carry THIS deliberate message, never the internals of a stray
    KeyError raised somewhere below (the I6 disclosure lens)."""


def settle_worked_hours(
    session: Session, run: PayRun, employee_id: int, *, actor: str, note: str,
) -> WageSettlement:
    """Record that the CURRENT worked-hours delta on one paid line was
    paid OUTSIDE the integration (decision 3). The server computes the
    delta itself — derived minus stored minus already-settled, the exact
    figure `_paid_worked_problems` would block on — and records exactly
    that: the caller acknowledges what IS, never a figure they typed, so
    a settlement can neither overshoot nor mask a later drift.

    Refusals: an unsubmitted run paid nothing (there is no delta against
    it); an unapproved card is a derivation in flux (settling would
    freeze a figure the re-approval is about to change — and the guard's
    re-approve blocker is not an hours delta, so settlement must not
    clear it); a zero-or-negative delta is an act with no effect (the F6
    acknowledgment rule — the schema's `hours > 0` CHECK refuses it
    independently). A missing line raises `SettlementNotFound`: this run
    paid nothing for that employee.

    The line row is locked (`FOR UPDATE`) for the delta computation (the
    I6 money lens): without it, two concurrent settlements both read
    settled=0 and both record the full delta — and the phantom excess
    then absorbs the NEXT genuinely unpaid drift silently. Serialized,
    the second computation reads the first's committed row and refuses
    with "nothing to settle"."""
    if run.submitted_at is None:
        raise SettlementRefused(
            f"pay run {run.pay_run_id} is {run.status}; it paid nothing, "
            "so there is no worked-hours delta against it to settle."
        )
    line = session.execute(
        select(PayRunLine).where(
            PayRunLine.pay_run_id == run.pay_run_id,
            PayRunLine.employee_id == employee_id,
        ).with_for_update()
    ).scalar_one_or_none()
    if line is None:
        raise SettlementNotFound(
            f"pay run {run.pay_run_id} has no line for employee "
            f"{employee_id}; it paid nothing for them."
        )
    state, derived, stored_worked, settled = _worked_hours_facts(
        session, run, line
    )
    if state == "unapproved":
        raise SettlementRefused(
            f"the timecard for the {run.period_start.isoformat()} period "
            "was reopened and never re-approved — re-approve it first, "
            "then settle the delta the approved card actually shows."
        )
    delta = (derived - stored_worked - settled).quantize(_CENTS)
    if delta <= 0:
        raise SettlementRefused(
            "nothing to settle: the card's current worked derivation does "
            "not exceed what the run paid plus what was already settled."
        )
    settlement = WageSettlement(
        pay_run_id=run.pay_run_id, employee_id=employee_id,
        hours=delta, actor_subject=actor, note=note,
    )
    session.add(settlement)
    session.flush()
    return settlement


def assemble_pay_run_entries(
    session: Session, property_id: str, in_period: date, *, anchor: date,
    provider_capabilities: ProviderCapabilities | None = None,
) -> PreflightReport:
    period_start, period_end = period_for(in_period, anchor)
    schedule = session.execute(
        select(PaySchedule).where(PaySchedule.property_id == property_id)
    ).scalar_one_or_none()
    check_date = (
        period_end + timedelta(days=schedule.check_date_offset_days)
        if schedule is not None else None
    )
    report = PreflightReport(property_id=property_id, period_start=period_start,
                             period_end=period_end, check_date=check_date)
    if schedule is None:
        report.problems.append(f"no pay schedule configured for property {property_id}")

    # One ruleset per run: every timecard in it belongs to this property, so
    # the jurisdiction is fixed for the whole run.
    prop = session.get(Property, property_id)
    if prop is None:
        report.problems.append(f"unknown property {property_id}")
        return report
    rules = rules_for(prop.wage_jurisdiction)

    # POPULATION BY PRIMARY ASSIGNMENT, not "works at this property". Once an
    # employee holds assignments at two properties, a "works here" predicate
    # matches their single timecard in BOTH properties' pay runs -- and they are
    # paid twice. Cost still reaches both hotels, allocated separately in
    # fetch_pay_run_results.
    #
    # PERIOD-scoped, not sampled at period_end. Someone terminated mid-period
    # holds no assignment on the last day, so a point-in-time population dropped
    # them and their approved worked hours were never submitted -- silently, with
    # nothing naming the employee.
    paid_here = employee_ids_paid_by_during(session, property_id, period_start, period_end)
    if paid_here:
        # Excluded staff (owners drawing no wage) hold real placements and may
        # hold real timecards, so the population predicate finds them — and
        # every per-employee check below (rate, sealed profile) would then name
        # them as a blocker for a condition that is by design. Filtered here
        # with a server-side log, NOT a preflight line: a permanent
        # "problem" in every preflight trains people to ignore preflight
        # (E3 decision 4).
        excluded = set(session.execute(
            select(Employee.employee_id).where(
                Employee.employee_id.in_(paid_here),
                Employee.pay_type == EXCLUDE_FROM_PAYROLL,
            )
        ).scalars())
        if excluded:
            # `pay_type` is undated, so "excluded" is only known NOW — it says
            # nothing about the period being paid. Promoted labor facts are the
            # evidence that distinguishes the two cases: an always-excluded
            # owner's cards promote NO facts (labor.py skips them), so facts on
            # an excluded person's card mean they were PRICED inside this very
            # period and dropping them silently loses earned wages — the E1
            # dropped-paycheck shape the comment above forbids. Those become
            # named blockers; the fact-less rest filter with a log (decision 4:
            # a permanent condition must not be a permanent preflight line).
            flipped = set(session.execute(
                select(Timecard.employee_id)
                .join(UsaliLaborFact,
                      UsaliLaborFact.timecard_id == Timecard.timecard_id)
                .where(
                    Timecard.employee_id.in_(excluded),
                    Timecard.period_start == period_start,
                )
                .distinct()
            ).scalars())
            for emp_id in sorted(flipped):
                emp = session.get(Employee, emp_id)
                assert emp is not None
                report.problems.append(
                    f"employee {emp_id} ({emp.full_name}): pay_type is "
                    "exclude_from_payroll, but this period already carries "
                    "promoted labor cost for them — they were payable when the "
                    "work was done. Excluding them now would silently drop "
                    "earned wages; resolve the classification or the filed "
                    "facts deliberately."
                )
            silent = excluded - flipped
            if silent:
                _LOG.info(
                    "pay run %s %s: filtering %d exclude_from_payroll "
                    "employee(s) from the population: %s",
                    property_id, period_start.isoformat(), len(silent),
                    sorted(silent),
                )
            # BOTH sets leave the population: a flipped employee must not be
            # paid as-is either — the blocker above is what stops the run.
            paid_here -= excluded

    # G3: sick leave taken this period, from the ONE derivation the SOS
    # reads (sick_days_taken — books and paychecks must not disagree about
    # the same hours). Grouped per employee; the card loop pops its people,
    # the post-loop pays whoever was out sick the whole period (no card).
    sick_by_emp: dict[int, list[SickDayTaken]] = {}
    for taken in sick_days_taken(session, period_start, period_end):
        sick_by_emp.setdefault(taken.employee_id, []).append(taken)
    report.problems.extend(_late_sick_problems(
        session, property_id, period_start
    ))
    report.problems.extend(_late_worked_problems(
        session, property_id, period_end
    ))
    report.problems.extend(_paid_worked_problems(
        session, property_id, period_start
    ))
    cards = (
        session.execute(
            select(Timecard).where(
                Timecard.employee_id.in_(paid_here),
                Timecard.period_start == period_start,
            )
        ).scalars().all()
        if paid_here
        else []
    )
    if not cards:
        report.problems.append(f"no timecards for {property_id} period {period_start}")
        return report

    for card in cards:
        employee = session.get(Employee, card.employee_id)
        assert employee is not None
        who = f"employee {employee.employee_id} ({employee.full_name})"
        if not employee.payroll_data_complete:
            # A NAMED blocker, deliberately not a population filter: a
            # filtered id vanishes from the run with nothing naming them (the
            # E1 dropped-paycheck shape), a blocker is a to-do item. Every
            # post-E3 hire starts false until a human states the paperwork is
            # in.
            report.problems.append(
                f"{who}: payroll data incomplete -- payroll_data_complete is "
                "false. Complete their record and set the flag before this "
                "run can pay them."
            )
            continue
        if card.status != "approved":
            report.problems.append(f"{who}: timecard {card.timecard_id} is not approved")
            continue
        # THE RATE, and the one shape this port cannot express. `PayRunEntry`
        # carries a SINGLE hourly_rate, but since E2 a rate belongs to a
        # placement and a date range -- so an employee who took a raise
        # mid-period, or who worked two placements paid differently, has no
        # single rate that is true. Resolved over the days and placements they
        # ACTUALLY worked, and refused rather than averaged or sampled.
        #
        # At the E2 cutover this can never fire: the backfill gave every
        # placement of one person the same rate from the one scalar. It fires
        # only once someone records genuinely different money, which is exactly
        # when a single field would start lying to the provider.
        try:
            distinct = _distinct_rates(session, card, employee.employee_id)
            premiums = _stored_premium_types(session, card, employee.employee_id)
            uncosted = _uncosted_worked_days(session, card, employee.employee_id)
        except (RateError, AmbiguousAssignmentError) as exc:
            # Every per-employee refusal -- an ambiguous placement, a rate that
            # is ambiguous, below the statutory floor, unverifiable, or corrupt
            # -- lands as a blocker naming THIS employee. `AmbiguousAssignmentError`
            # is NOT a RateError (it comes from `assignment_at`), so it needs its
            # own name here; letting either propagate would 500 the whole run and
            # name nobody, blocking every other employee's paycheck.
            report.problems.append(f"{who}: {exc}")
            continue
        sick_days = sick_by_emp.pop(employee.employee_id, [])
        sick_total, sick_rate, sick_problems = _resolve_employee_sick(
            sick_days, property_id, who
        )
        if sick_problems:
            report.problems.extend(sick_problems)
            continue
        if not distinct and sick_total == 0:
            report.problems.append(f"{who}: no pay rate on file")
            continue
        if uncosted:
            # A placement exists for these days but no rate is in force on them.
            # Paying them at the covered days' rate would submit money the
            # resolver refused to name -- fix the rate dates, or pay the period
            # outside the integration.
            report.problems.append(
                f"{who}: worked {', '.join(d.isoformat() for d in uncosted)} with "
                "no pay rate in force on those dates; a placement exists but its "
                "rate does not cover them. Submitting would pay those hours at "
                "another day's rate. Correct the rate's effective dates."
            )
            continue
        if len(distinct) > 1:
            report.problems.append(
                f"{who}: {len(distinct)} different pay rates are in force across "
                "the days worked in this period, and a pay run submits one rate "
                "per employee. Refusing to average or to pick: either would "
                "submit money nobody authorised. Split the period at the rate "
                "change, or correct the rates."
            )
            continue
        if premiums:
            report.problems.append(
                f"{who}: a stored {'/'.join(sorted(premiums))} premium rate "
                "cannot be submitted through this payroll port, which carries a "
                "single hourly rate and lets the provider apply the statutory "
                "multiplier. Submitting anyway would pay the statutory figure "
                "and silently drop the premium. Remove the stored premium, or "
                "pay this period outside the integration."
            )
            continue
        if distinct and sick_rate is not None and sick_rate not in distinct:
            # The sick days were taken at a rate the worked days do not
            # share (a raise dated between them). One entry, one rate: the
            # _distinct_rates refusal, extended across the worked/sick line.
            report.problems.append(
                f"{who}: the sick days taken this period are priced at a "
                "different rate than the days worked, and a pay run submits "
                "one rate per employee. Refusing to average or to pick. "
                "Split the period at the rate change, or correct the rates."
            )
            continue

        person_problems = _person_problems(
            session, employee, who, provider_capabilities
        )
        if person_problems:
            report.problems.extend(person_problems)
            continue

        day_hours = {
            d.business_date: (Decimal(d.worked_minutes) / Decimal("60")).quantize(_CENTS)
            for d in compute_timecard(session, card)
            if d.worked_minutes > 0
        }
        stacked = _stacked_sick_problems(
            session, employee.employee_id, who, day_hours,
            [t for t in sick_days if t.property_id == property_id],
        )
        if stacked:
            report.problems.extend(stacked)
            continue
        # Resolved over the days ACTUALLY WORKED, not sampled at period_start.
        # This is the path that submits money, and the previous fix for this bug
        # landed only on the estimate in labor.py -- $160 of unpaid overtime
        # shipped from right here.
        try:
            exempt = resolve_exemption(session, employee.employee_id, day_hours)
        except MixedExemptionError as exc:
            report.problems.append(f"{who}: {exc}")
            continue
        reg = ot = dt = Decimal("0")
        for row in compute_overtime(
            day_hours, anchor=anchor, exempt=exempt, rules=rules
        ):
            reg += row.regular_hours
            ot += row.ot_hours
            dt += row.dt_hours
        # The worked rate when any day was worked; a card that carries only
        # sick hours (all worked days at the other property, or none at
        # all) is priced by the sick days' own rate — the mismatch refusal
        # above guarantees they agree whenever both exist.
        rate = distinct.pop() if distinct else sick_rate
        assert rate is not None
        # G3 (decision 8): the estimate includes sick pay — an estimate that
        # ignored hours the submission pays would re-open the E1
        # fix-one-side shape on the carve check and the variance.
        est_gross = rate * (
            reg
            + ot * rules.ot_multiplier
            + dt * (rules.dt_multiplier if rules.dt_multiplier is not None else 1)
            + sick_total
        )
        carve = _carve_problem(session, employee.employee_id, who, est_gross)
        if carve is not None:
            report.problems.append(carve)
            continue
        report.entries.append(PreflightEntry(
            employee_id=employee.employee_id,
            regular_hours=reg.quantize(_CENTS), ot_hours=ot.quantize(_CENTS),
            dt_hours=dt.quantize(_CENTS), hourly_rate=rate,
            sick_hours=sick_total.quantize(_CENTS),
        ))

    # G3: whoever was out sick the WHOLE period has usage but no card — the
    # timecard loop above never saw them, and before G3 their hours
    # silently reached nobody (the E4 F5 finding, one layer deeper). Same
    # person-guards as the card path, zeros for worked hours. Card holders
    # are NOT re-processed here: a card-path refusal already names them,
    # and paying a refused person's sick hours alone would submit a person
    # the preflight just blocked.
    card_employees = {c.employee_id for c in cards}
    for employee_id in sorted(sick_by_emp):
        if employee_id in card_employees:
            continue
        days = sick_by_emp[employee_id]
        here_days = [t for t in days if t.property_id == property_id]
        employee = session.get(Employee, employee_id)
        assert employee is not None
        who = f"employee {employee_id} ({employee.full_name})"
        if employee_id not in paid_here:
            # Only days actually attributed HERE are this run's problem; an
            # unattributable day of someone paid elsewhere is the SOS
            # warning's territory, and another property's days are its own
            # run's.
            if here_days:
                report.problems.append(
                    f"{who}: sick leave taken this period is attributed to "
                    "this property, but this run does not pay this employee "
                    "(excluded from payroll, or the primary placement "
                    "moved). Resolve the classification, the placement, or "
                    "the entry deliberately — these hours are otherwise "
                    "paid by nobody."
                )
            continue
        if not any(t.property_id in (property_id, None) for t in days):
            continue  # entirely another property's to pay
        if not employee.payroll_data_complete:
            report.problems.append(
                f"{who}: payroll data incomplete -- payroll_data_complete is "
                "false. Complete their record and set the flag before this "
                "run can pay them."
            )
            continue
        sick_total, sick_rate, sick_problems = _resolve_employee_sick(
            days, property_id, who
        )
        if sick_problems:
            report.problems.extend(sick_problems)
            continue
        if sick_total == 0:
            continue  # fully voided
        assert sick_rate is not None
        person_problems = _person_problems(
            session, employee, who, provider_capabilities
        )
        if person_problems:
            report.problems.extend(person_problems)
            continue
        carve = _carve_problem(
            session, employee_id, who, sick_rate * sick_total
        )
        if carve is not None:
            report.problems.append(carve)
            continue
        report.entries.append(PreflightEntry(
            employee_id=employee_id,
            regular_hours=Decimal("0.00"), ot_hours=Decimal("0.00"),
            dt_hours=Decimal("0.00"), hourly_rate=sick_rate,
            sick_hours=sick_total.quantize(_CENTS),
        ))

    # G5 (§246(i)): every paid entry carries the available balance as of
    # the CHECK DATE — zero included; only when the provider DECLARED it
    # can render the figure on the stub. An undeclared capability gets
    # None ("do not display"), never a figure the provider would silently
    # drop — and both real adapters refuse a balance-carrying entry until
    # the go-live sandbox verifies them, so a mis-gated caller fails loud.
    if (
        provider_capabilities is not None
        and provider_capabilities.supports_sick_balance_display
        and check_date is not None
    ):
        report.entries = [
            replace(e, sick_balance_hours=balance_on(
                session, e.employee_id, check_date))
            for e in report.entries
        ]
    return report


def _worked_placements(
    session: Session, card: Timecard, employee_id: int
) -> list[tuple[int, date]]:
    """(assignment_id, business_date) for every placement-day this card covers.

    Keyed on WORKED placements rather than every placement in force: someone
    assigned to both hotels but who worked only one of them this period has one
    rate governing their pay, and blocking on the other property's rate would
    refuse a pay run that is perfectly expressible.
    """
    out: list[tuple[int, date]] = []
    for day in compute_timecard(session, card):
        if day.worked_minutes <= 0:
            continue
        for property_id in day.property_minutes:
            if property_id is None:
                # Unattributed time — no placement, so no rate to read. The
                # hours are still paid at whatever the attributed days resolve
                # to; they simply cannot ADD a rate to the distinct set.
                continue
            assignment = assignment_at(
                session, employee_id, property_id, day.business_date
            )
            if assignment is not None:
                out.append((assignment.assignment_id, day.business_date))
    return out


def _distinct_rates(
    session: Session, card: Timecard, employee_id: int
) -> set[Decimal]:
    """Every distinct `regular` rate governing the days this card actually
    covers."""
    rates: set[Decimal] = set()
    for assignment_id, business_date in _worked_placements(
        session, card, employee_id
    ):
        rate = rate_on(session, assignment_id, "regular", business_date)
        if rate is not None:
            rates.add(rate)
    return rates


def _uncosted_worked_days(
    session: Session, card: Timecard, employee_id: int
) -> list[date]:
    """Worked days that have a PLACEMENT but no `regular` rate in force on them.

    The distinction is the whole point. `_worked_placements` already excludes
    unattributed time (no placement -> not payable through a rate, documented).
    What remains is a real placement whose rate does not cover the day worked --
    a rate that starts later, or ends earlier. `_distinct_rates` DROPS those
    (its `if rate is not None`), so a card with some covered and some uncovered
    days collapses to one distinct rate and pays EVERY hour at it, including the
    hours the resolver said could not be costed. That is the silent sample
    `rates.py` forbids: `None` means cannot cost, never a nearby rate.
    """
    return sorted(
        business_date
        for assignment_id, business_date in _worked_placements(
            session, card, employee_id
        )
        if rate_on(session, assignment_id, "regular", business_date) is None
    )


def _stored_premium_types(
    session: Session, card: Timecard, employee_id: int
) -> set[str]:
    """Premium rate types (ot/dot/holiday) STORED on the placements worked.

    This port cannot carry them. `PayRunEntry` sends one `hourly_rate` and the
    provider applies its own statutory multipliers, so a stored union or
    contract premium would be honoured by the ESTIMATE and silently discarded by
    the SUBMISSION -- the estimate/actual variance would then show a permanent
    gap that is really an underpayment.

    That is the shape the plan warned about in as many words: E1 fixed the
    estimate and left this path shipping $160 of unpaid overtime, "fix both
    together or neither". Carrying premiums to the provider needs a port change
    and a provider that accepts them, so until then this REFUSES rather than
    quietly pays the statutory figure.

    Nothing backfilled these, so at the E2 cutover the set is always empty.

    Resolving them here also re-checks the statutory floor on the path that
    actually pays -- defence in depth, exactly as the module docstring for
    `usali.rates` describes.
    """
    stored: set[str] = set()
    for assignment_id, business_date in _worked_placements(
        session, card, employee_id
    ):
        for rate_type in ("ot", "dot", "holiday"):
            if rate_on(session, assignment_id, rate_type, business_date) is not None:
                stored.add(rate_type)
    return stored


class PayRunBlocked(ValueError):
    """Preflight failed; nothing was created and no provider call was made."""


class PayRunConflict(PayRunBlocked):
    """A non-failed pay run already exists for this property + period (409)."""


class SealedFieldError(ValueError):
    """A sealed envelope failed to parse or open in its derived slot. Carries
    the FIELD NAME only — never the envelope, the aad, or any plaintext (all
    of this reaches logs and API error details)."""

    def __init__(self, field_name: str) -> None:
        self.field_name = field_name
        super().__init__(field_name)


def payload_fingerprint(session: Session, employee_id: int) -> str:
    """SHA-256 hex over EXACTLY the fields the sync payload ships (H7,
    decision 8): full_name, the sealed ssn ciphertext, and the ordered
    chain rows' (ordinal, allocation_type, allocation_value, account_type,
    sealed_routing, sealed_account). Computed WITHOUT opening anything —
    ciphertext identity IS payload identity, so a re-seal of the same
    plaintext honestly reads as an edit (it is one: key rotation or
    re-entry) while the server never touches plaintext to answer a
    bookkeeping question.

    The field list is a DISCLOSURE SURFACE in reverse: a field the payload
    does not ship (tax elections) must never enter this hash, or an
    unshipped edit would re-send SSN + chain through transit for nothing —
    the exact G7 PII residual this retires. NUL separators keep adjacent
    values from concatenating into a collision."""
    employee = session.get(Employee, employee_id)
    assert employee is not None
    ssn_sealed = session.execute(
        select(EmployeePayrollProfile.ssn_sealed).where(
            EmployeePayrollProfile.employee_id == employee_id
        )
    ).scalar_one_or_none()
    chain = session.execute(
        select(DepositAccount)
        .where(DepositAccount.employee_id == employee_id)
        .order_by(DepositAccount.ordinal)
    ).scalars().all()
    digest = hashlib.sha256()
    for part in (employee.full_name, ssn_sealed or ""):
        digest.update(part.encode())
        digest.update(b"\x00")
    for row in chain:
        for value in (row.ordinal, row.allocation_type, row.allocation_value,
                      row.account_type, row.sealed_routing, row.sealed_account):
            digest.update(str(value).encode())
            digest.update(b"\x00")
    return digest.hexdigest()


def provider_payload_stale(
    session: Session, employee_id: int, ref: ProviderEmployeeRef
) -> bool:
    """Does the provider hold what we would send today? THE staleness
    predicate — ONE copy (G2), consumed by the preflight blocker, the
    re-sync in `sync_employees`, and nothing else.

    H7 (decision 8): stored fingerprint != current fingerprint. Content,
    not clocks — the created_at-vs-synced_at comparison retires, and with
    it the transaction-start visibility race and the equal-timestamp
    boundary (S9): a payload synced in the same transaction that wrote
    the rows has the same fingerprint by construction, whatever any clock
    says. NULL (every pre-H ref — the H2 migration deliberately backfills
    none) reads STALE: one re-send self-heals it. Fail toward the extra
    send, never the skipped one. `synced_at` is display-only now."""
    return ref.payload_fingerprint is None or (
        ref.payload_fingerprint != payload_fingerprint(session, employee_id)
    )


def _stale_provider_payload_problems(
    session: Session, employee_id: int, who: str
) -> list[str]:
    """The named blocker for a stale payload at a provider we cannot
    update: paying by the PROVIDER'S old copy while preflight green-lights
    the DB's new one is active false assurance (the E5 review's finding),
    so it blocks BY NAME — loudly wrong beats quietly wrong. Skipped by the
    caller when the provider supports update (G2): then the staleness is
    healed by re-sync, not refused. First-run employees have no ref yet —
    no blocker; their first sync sends the current payload."""
    stale_refs = sorted(
        r.provider
        for r in session.execute(
            select(ProviderEmployeeRef).where(
                ProviderEmployeeRef.employee_id == employee_id
            )
        ).scalars()
        if provider_payload_stale(session, employee_id, r)
    )
    if not stale_refs:
        return []
    return [
        f"{who}: the deposit chain or payroll profile changed after the "
        f"provider record was created ({', '.join(stale_refs)}); the "
        "provider still holds the OLD payload and cannot be updated. "
        "Re-establish the provider employee record, or restore the prior "
        "values, before this run can pay them."
    ]


def _open_field(opener: Opener, employee_id: int, field_name: str, sealed: str) -> str:
    """Parse + open one envelope in its derived slot, or raise
    `SealedFieldError(field_name)`. The broad except is deliberate: HPKE
    library failures (wrong aad, wrong key, corrupt ciphertext) and
    `EnvelopeError` parses must all become ONE domain error that carries the
    field name only — the raw exception chain is preserved for server logs,
    but nothing that travels upward may hold the envelope, the aad, or any
    plaintext. Before this, a mis-sealed record escaped as an uncaught HPKE
    error that 500'd the whole run and named nobody (the E5 review's
    finding — the same shape E2 fixed for rate errors)."""
    try:
        env = SealedEnvelope.parse(sealed)
        return opener.open(
            env, aad=f"{employee_id}:{field_name}".encode()
        ).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — translated to a named domain error
        raise SealedFieldError(field_name) from exc


def _build_payroll_employee(
    session: Session, opener: Opener, employee_id: int
) -> PayrollEmployee:
    """Open the sealed vault into ONE transient payload — the only place
    plaintext PII exists. Shared by first sync and re-sync (G2): two copies
    of this would be two chances for the slot-binding to drift.

    PER ROW, with the aad derived from ROW IDENTITY (ordinal + field,
    legacy slot for backfilled rows). A spliced envelope — right key,
    wrong slot — fails the HPKE auth here and the run REFUSES with a
    blocker NAMING this employee (never the value, the envelope, or
    the aad), rather than depositing a paycheck into whichever account
    the ciphertext was moved from — or 500ing the whole run and
    blocking everyone else's paycheck, which is what an uncaught HPKE
    error did before the E5 review caught it."""
    employee = session.get(Employee, employee_id)
    profile = session.execute(
        select(EmployeePayrollProfile).where(
            EmployeePayrollProfile.employee_id == employee_id
        )
    ).scalar_one()
    assert employee is not None and profile.ssn_sealed is not None
    chain = list(session.execute(
        select(DepositAccount)
        .where(DepositAccount.employee_id == employee_id)
        .order_by(DepositAccount.ordinal)
    ).scalars())
    assert chain, "preflight guarantees a chain before anything is sent"
    try:
        return PayrollEmployee(
            full_name=employee.full_name,
            ssn=_open_field(opener, employee_id, "ssn", profile.ssn_sealed),
            deposit_accounts=tuple(
                PlainDepositAccount(
                    ordinal=r.ordinal,
                    allocation_type=r.allocation_type,
                    allocation_value=r.allocation_value,
                    account_type=r.account_type,
                    routing=_open_field(
                        opener, employee_id,
                        routing_slot(r.ordinal, r.legacy_sealed),
                        r.sealed_routing,
                    ),
                    account=_open_field(
                        opener, employee_id,
                        account_slot(r.ordinal, r.legacy_sealed),
                        r.sealed_account,
                    ),
                )
                for r in chain
            ),
            # NOT opened (G7 PII review): neither adapter serializes tax
            # elections, so opening the sealed value put plaintext in
            # transit that could never reach the wire — pure exposure, no
            # carrier. Stays sealed at rest until an adapter grows a tax
            # payload (a sandbox-verified wire shape, not an invented one).
            tax_elections=None,
        )
    except SealedFieldError as exc:
        raise PayRunBlocked(
            f"employee {employee_id} ({employee.full_name}): sealed field "
            f"'{exc.field_name}' failed to open in its slot -- the "
            "envelope does not match this employee/ordinal/field "
            "(mis-sealed, moved, or sealed to a rotated key). Re-enter "
            "and re-seal the value. Nothing was sent to the provider."
        ) from exc


def sync_employees(
    session: Session, provider: PayrollProvider, opener: Opener, *,
    provider_name: str, employee_ids: list[int],
) -> dict[int, str]:
    """Ensure every employee has a provider-side id AND that the provider's
    copy is current; returns employee_id -> provider_employee_id. Opens
    sealed PII ONLY here, transiently, per field — and only for employees
    who NEED a send (first sync or stale); an unchanged person's vault
    stays closed (G2: re-sync must not widen the PII-transit surface).

    A stale ref (provider_payload_stale — the one predicate) re-sends the
    full payload via update_employee and bumps synced_at ONLY on success:
    a bumped timestamp without a delivered payload would hide the
    staleness forever. Update failure and update-less providers both
    refuse by name — the run must never submit against a copy the
    employee has replaced."""
    ref_rows = {
        r.employee_id: r
        for r in session.execute(
            select(ProviderEmployeeRef).where(
                ProviderEmployeeRef.provider == provider_name,
                ProviderEmployeeRef.employee_id.in_(employee_ids),
            )
        ).scalars()
    }
    refs = {eid: r.provider_employee_id for eid, r in ref_rows.items()}
    for employee_id in employee_ids:
        existing = ref_rows.get(employee_id)
        if existing is not None:
            if not provider_payload_stale(session, employee_id, existing):
                continue
            employee = session.get(Employee, employee_id)
            assert employee is not None
            who = f"employee {employee_id} ({employee.full_name})"
            if not provider.capabilities().supports_employee_update:
                # Preflight already refuses this by name when told the
                # capabilities; defence in depth for callers that reach
                # sync another way.
                raise PayRunBlocked(
                    "; ".join(_stale_provider_payload_problems(
                        session, employee_id, who
                    ))
                )
            # Fingerprint and payload read the same transaction snapshot,
            # so the stored fingerprint describes exactly what was sent.
            # Stored ONLY on success (like synced_at): a fingerprint
            # without a delivered payload would hide the staleness forever.
            fingerprint = payload_fingerprint(session, employee_id)
            payload = _build_payroll_employee(session, opener, employee_id)
            try:
                provider.update_employee(existing.provider_employee_id, payload)
            except ProviderError as exc:
                # Adapter sync-path messages are PII-free by construction
                # (fixed strings, no response detail).
                raise PayRunBlocked(
                    f"{who}: provider update failed ({exc}); the provider "
                    "still holds the old payload and this run must not pay "
                    "by it. Retry once the provider recovers."
                ) from exc
            existing.payload_fingerprint = fingerprint
            existing.synced_at = func.now()
            continue
        fingerprint = payload_fingerprint(session, employee_id)
        payroll_employee = _build_payroll_employee(session, opener, employee_id)
        provider_employee_id = provider.sync_employee(payroll_employee)
        session.add(ProviderEmployeeRef(
            employee_id=employee_id, provider=provider_name,
            provider_employee_id=provider_employee_id,
            payload_fingerprint=fingerprint,
        ))
        refs[employee_id] = provider_employee_id
    session.flush()
    return refs


def execute_pay_run(
    session: Session, property_id: str, in_period: date, *, anchor: date,
    provider: PayrollProvider, provider_name: str, opener: Opener, actor: str,
) -> PayRun:
    """Preflight -> (replace a failed run) -> sync -> submit. Raises PayRunBlocked
    on preflight problems (nothing created), PayRunConflict if a non-failed run
    already exists for the period; a ProviderError from EITHER sync or submit
    marks the run failed (failure_reason is PII-free by construction — the
    adapters strip response detail on the sync path). On submit success the
    per-employee hours are snapshotted into PayRunLine rows immediately
    (money = encrypted placeholder "0" until fetch) so a later timecard change
    can never desync stored hours from what the provider priced."""
    report = assemble_pay_run_entries(
        session, property_id, in_period, anchor=anchor,
        provider_capabilities=provider.capabilities(),
    )
    if not report.ok:
        raise PayRunBlocked("; ".join(report.problems) or "no entries")
    assert report.check_date is not None

    existing = session.execute(
        select(PayRun).where(PayRun.property_id == property_id,
                             PayRun.period_start == report.period_start)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status != "failed":
            raise PayRunConflict(
                f"a pay run for {property_id} {report.period_start} already exists "
                f"(status {existing.status})"
            )
        session.execute(delete(PayRunLine).where(PayRunLine.pay_run_id == existing.pay_run_id))
        # A failed run has neither lines nor census (both write on submit
        # success only) — deleted defensively together regardless.
        session.execute(delete(PayRunLineProperty).where(
            PayRunLineProperty.pay_run_id == existing.pay_run_id
        ))
        session.delete(existing)
        session.flush()

    run = PayRun(property_id=property_id, period_start=report.period_start,
                 period_end=report.period_end, check_date=report.check_date,
                 status="draft", provider=provider_name, created_by=actor)
    session.add(run)
    session.flush()

    try:
        refs = sync_employees(session, provider, opener, provider_name=provider_name,
                              employee_ids=[e.employee_id for e in report.entries])
        # G7 (money B): the preflight's derivation is seconds old by now —
        # sync round-trips the provider — and a sick usage (or timecard)
        # recorded in between is provably NOT in `report.entries`, while
        # its created_at would still precede submitted_at, so the §246(n)
        # guard would forever call it paid. Re-assemble and refuse to send
        # anything that no longer matches: the run fails LOUDLY before the
        # provider is touched, replaceable like any failed run. Residual
        # window: a write during the submit HTTP call itself — recorded in
        # the backlog with the transaction-timestamp race family.
        recheck = assemble_pay_run_entries(
            session, property_id, in_period, anchor=anchor,
            provider_capabilities=provider.capabilities(),
        )

        def _keyed(entries: list[PreflightEntry]) -> list[PreflightEntry]:
            return sorted(entries, key=lambda e: e.employee_id)

        if recheck.problems or _keyed(recheck.entries) != _keyed(report.entries):
            run.status = "failed"
            run.failure_reason = (
                "preflight drifted during submission (a timecard or sick "
                "entry changed after assembly); nothing was sent — re-run "
                "the period"
            )
            session.add(AuditEvent(actor_subject=actor, action="pay_run_failed",
                                   resource_type="pay_run",
                                   resource_id=str(run.pay_run_id)))
            session.flush()
            return run
        entries = [
            PayRunEntry(provider_employee_id=refs[e.employee_id],
                        regular_hours=e.regular_hours, ot_hours=e.ot_hours,
                        dt_hours=e.dt_hours, hourly_rate=e.hourly_rate,
                        sick_hours=e.sick_hours,
                        sick_balance_hours=e.sick_balance_hours)
            for e in report.entries
        ]
        provider_run = provider.submit_pay_run(
            period_start=report.period_start, period_end=report.period_end,
            check_date=report.check_date, entries=entries,
        )
    except ProviderError as exc:
        run.status = "failed"
        run.failure_reason = str(exc)[:300]
        session.add(AuditEvent(actor_subject=actor, action="pay_run_failed",
                               resource_type="pay_run", resource_id=str(run.pay_run_id)))
        session.flush()
        return run
    run.status = "submitted"
    run.provider_run_id = provider_run.provider_run_id
    # Record-keeping only since H6: the §246(n) guard is content-based
    # (stored lines vs the ledger) and no longer reads this. DB clock
    # kept for consistency with the ledger's server-generated stamps.
    run.submitted_at = session.execute(select(func.now())).scalar_one()
    # Snapshot the SUBMITTED hours now (only after submit success — a failed
    # run must have no lines so replacement stays trivial). Money fields are
    # non-null encrypted strings; "0" is the pre-fetch placeholder.
    for entry in report.entries:
        total = (entry.regular_hours + entry.ot_hours + entry.dt_hours
                 + entry.sick_hours)
        session.add(PayRunLine(
            pay_run_id=run.pay_run_id, employee_id=entry.employee_id,
            # PAID hours, sick included — this snapshot is "hours this run
            # paid", and G3 makes sick hours paid hours.
            hours=total,
            # H5 (decision 5): the sick share of those hours, stored as its
            # own fact at the same moment. "What did this run pay as sick"
            # stops being a re-derivation over a ledger that keeps moving —
            # the apportionment (decision 6) and the §246(n) guard (H6)
            # read THIS, never the ledger.
            sick_hours=entry.sick_hours,
            gross="0", employee_taxes="0", employer_taxes="0", net="0",
        ))
        # I1 (decision 1): the census is a SUBMIT fact too — the per-property
        # split of those hours, frozen with everything else the run pays by.
        # Money zeroed until fetch prices it (zero gross keeps the rows
        # invisible to the suppression gate — an unfetched run discloses
        # nothing); department NULL until fetch resolves it per property.
        weights = _line_weights(
            session, property_id, entry.employee_id, report.period_start,
            total, entry.sick_hours,
        )
        for prop, share in sorted(
            apportion(total, weights, quantum=_CENTS).items()
        ):
            session.add(PayRunLineProperty(
                pay_run_id=run.pay_run_id, employee_id=entry.employee_id,
                property_id=prop, department_id=None,
                hours=share, gross=Decimal("0"),
                employer_burden=Decimal("0"),
            ))
    session.add(AuditEvent(actor_subject=actor, action="submit_pay_run",
                           resource_type="pay_run", resource_id=str(run.pay_run_id)))
    session.flush()
    return run


def _line_weights(
    session: Session, run_property: str, employee_id: int,
    period_start: date, total_hours: Decimal, sick_hours: Decimal,
) -> dict[str, Decimal]:
    """ONE copy of a pay line's apportionment weights (the H5 rules,
    hoisted for I1 so submit and fetch's self-heal cannot drift): the
    timecard's worked hours per property, plus the line's sick hours
    credited to the run's own property (G3 refuses sick attributed
    elsewhere, so every paid sick hour is by construction this
    property's); a line with no card and no worked weights (the
    sick-only entrant) falls back wholly to the paying property."""
    card = session.execute(
        select(Timecard).where(
            Timecard.employee_id == employee_id,
            Timecard.period_start == period_start,
        )
    ).scalar_one_or_none()
    weights = property_hours_for_timecard(session, card) if card is not None else {}
    if sick_hours > 0:
        weights[run_property] = (
            weights.get(run_property, Decimal("0")) + sick_hours
        )
    if not weights:
        weights = {run_property: total_hours if total_hours > 0 else Decimal("1")}
    return weights


def fetch_pay_run_results(session: Session, run: PayRun, *, provider: PayrollProvider) -> int:
    """Pull the provider's result; UPDATE the run's submit-time PayRunLine rows
    (hours were snapshotted at submit — never recomputed from live timecards,
    which may have changed since) with the provider's encrypted money, and
    price the run's SUBMIT-TIME census rows in place (I1: money apportioned
    by the stored per-property hours — the split is a fact of the submission,
    so a reopen + relink between submit and fetch moves nothing). A run
    submitted before I1 has no stored rows: its first fetch derives the split
    live one last time and WRITES it (the NULL-fingerprint posture). The
    department aggregates stay delete-then-insert. Idempotent: re-fetching
    updates money to the same values."""
    if run.provider_run_id is None:
        raise ValueError(f"pay run {run.pay_run_id} was never submitted")
    result = provider.get_pay_run(run.provider_run_id)
    if result.status != "processed":
        return 0

    by_provider_id = {
        r.provider_employee_id: r.employee_id
        for r in session.execute(
            select(ProviderEmployeeRef).where(ProviderEmployeeRef.provider == run.provider)
        ).scalars()
    }
    lines_by_employee = {
        line.employee_id: line
        for line in session.execute(
            select(PayRunLine).where(PayRunLine.pay_run_id == run.pay_run_id)
        ).scalars()
    }
    session.execute(delete(UsaliActualLaborFact).where(
        UsaliActualLaborFact.pay_run_id == run.pay_run_id
    ))
    # I1: the census is NOT rebuilt — it was written at submit (hours split,
    # zero money) and is priced here in the same transaction as the facts,
    # so headcount and figures still cannot disagree. Only a pre-I run,
    # whose census was never written, gets a one-time live derivation below.
    census_by_emp: dict[int, list[PayRunLineProperty]] = {}
    for row in session.execute(
        select(PayRunLineProperty).where(
            PayRunLineProperty.pay_run_id == run.pay_run_id
        )
    ).scalars():
        census_by_emp.setdefault(row.employee_id, []).append(row)

    agg: dict[tuple[str, int | None], list[Decimal]] = {}
    written = 0
    for line in result.lines:
        employee_id = by_provider_id.get(line.provider_employee_id)
        stored = lines_by_employee.get(employee_id) if employee_id is not None else None
        if employee_id is None or stored is None:
            raise ValueError(
                f"provider returned unknown employee ref {line.provider_employee_id}"
            )
        stored.gross = str(line.gross)
        stored.employee_taxes = str(line.employee_taxes)
        stored.employer_taxes = str(line.employer_taxes)
        stored.net = str(line.net)
        hours = Decimal(str(stored.hours))
        employee = session.get(Employee, employee_id)
        assert employee is not None
        # Snapshot the SAME live department the aggregation below uses at this
        # moment: reporting-side employee counts (keyed on this snapshot) and
        # the department money can never disagree about attribution, even if
        # the employee later transfers.
        primary = primary_assignment_on(session, employee_id, run.period_end)
        stored.department_id = primary.department_id if primary is not None else None

        # ONE PAYCHECK, TWO P&Ls — split by the SUBMIT-TIME census. The rows
        # carry the per-property hours the run actually paid; the money
        # follows them, never the live card (which may have gained relinked
        # punches since — those are the worked-content guard's to name, not
        # this booking's to absorb).
        rows = census_by_emp.get(employee_id)
        if not rows:
            # A pre-I run: derive the split live ONE last time and write it.
            weights = _line_weights(
                session, run.property_id, employee_id, run.period_start,
                hours, Decimal(str(stored.sick_hours)),
            )
            rows = [
                PayRunLineProperty(
                    pay_run_id=run.pay_run_id, employee_id=employee_id,
                    property_id=prop, department_id=None,
                    hours=share, gross=Decimal("0"),
                    employer_burden=Decimal("0"),
                )
                for prop, share in sorted(
                    apportion(hours, weights, quantum=_CENTS).items()
                )
            ]
            session.add_all(rows)

        hour_weights = {
            row.property_id: Decimal(str(row.hours)) for row in rows
        }
        gross_shares = apportion(line.gross, hour_weights, quantum=_CENTS)
        burden_shares = apportion(line.employer_taxes, hour_weights,
                                  quantum=_CENTS)
        for row in sorted(rows, key=lambda r: r.property_id):
            # The department of the assignment AT THIS PROPERTY, not the
            # primary's. Departments are property-scoped (unique property_id +
            # name), so reusing the primary's id filed the other hotel's money
            # under a department belonging to a different hotel -- 58033's $88
            # booked to SJCES's Front Office. labor.py::department_at has
            # always resolved this per property; this path did not.
            department_id = department_at(
                session, employee_id, row.property_id, run.period_end
            )
            row.department_id = department_id
            row.gross = gross_shares[row.property_id]
            row.employer_burden = burden_shares[row.property_id]
            key = (row.property_id, department_id)
            bucket = agg.setdefault(key, [Decimal("0"), Decimal("0"), Decimal("0")])
            bucket[0] += Decimal(str(row.hours))
            bucket[1] += gross_shares[row.property_id]
            bucket[2] += burden_shares[row.property_id]
        written += 1

    for (property_id, department_id), (hours, gross, burden) in agg.items():
        session.add(UsaliActualLaborFact(
            property_id=property_id, period_start=run.period_start,
            period_end=run.period_end, department_id=department_id,
            hours=hours, gross=gross, employer_burden=burden,
            pay_run_id=run.pay_run_id,
        ))
    run.status = "processed"
    run.processed_at = datetime.now(UTC)
    session.flush()
    return written


# _weighted retired by I1: the hours split is a submit fact (`_line_weights`
# + `apportion` at execute), and fetch apportions money by the STORED census
# hours — each measure still sums exactly to the line via `apportion`.
