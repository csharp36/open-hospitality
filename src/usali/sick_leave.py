"""Sick-leave accrual and the balance fold (E4).

The ledger (`SickLeaveLedger`) is append-only facts; everything here is a
pure function of it. Three properties are load-bearing, each the fix for a
reproduced review finding:

- **Caps are recorded per entry, not derived at read time.** Every positive
  entry carries `cap_hours` — the accrual cap in force on ITS date, derived
  from the facts as of that date. Folding at today's cap extinguished
  lawfully banked hours whenever an employee's day length decayed (the
  statute review's Critical: an unlawful denial), and made a quiet quarter
  silently restate the reported balance (the money review's F3).
- **The fold is DAY-granular.** Entries of one `effective_on` sum before the
  clamp, so within-day order — which a re-promote REWRITES by handing the
  accrual a new entry_id — cannot change the answer (money F1: a routine
  re-promote moved a cap-adjacent balance).
- **Overdraw is judged over the whole timeline.** A backdated usage that
  passes a point-in-time balance check can still drive a LATER date
  negative (money F2); `would_overdraw` folds the hypothetical ledger and
  refuses if any day ends below zero.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from usali.models import SickLeaveLedger, Timecard, UsaliLaborFact
from usali.sick_leave_rules import sick_leave_rules_for

_LOG = logging.getLogger(__name__)
_HOURS = Decimal("0.01")
_EIGHT = Decimal("8")
_DAY_LENGTH_LOOKBACK_DAYS = 90


def day_length(session: Session, employee_id: int, on: date) -> Decimal | None:
    """The employee's day length D for the greater-of-days caps: average
    hours per WORKED day over the trailing 90 calendar days of PROMOTED
    (= approved) timecards, floored at 8 — the days-based figures may only
    RAISE the statutory 40/80 floors, never reduce them.

    Returns None when NO promoted facts exist in the window (cutover, new
    hire, >90-day absence). None means "no data", and every consumer treats
    it employee-favourably — no cap is applied — because the statute review
    reproduced the alternative: flooring to 8 with no data denied a
    10-hour/day worker their lawful 50-hour usage floor at the pilot's own
    documented cutover."""
    per_day = session.execute(
        select(func.sum(UsaliLaborFact.hours))
        .join(Timecard, Timecard.timecard_id == UsaliLaborFact.timecard_id)
        .where(
            Timecard.employee_id == employee_id,
            UsaliLaborFact.business_date > on - timedelta(days=_DAY_LENGTH_LOOKBACK_DAYS),
            UsaliLaborFact.business_date <= on,
        )
        .group_by(UsaliLaborFact.business_date)
    ).scalars().all()
    if not per_day:
        return None
    avg = (
        sum((Decimal(str(h)) for h in per_day), Decimal("0")) / len(per_day)
    ).quantize(_HOURS, rounding=ROUND_HALF_UP)
    return max(_EIGHT, avg)


def cap_hours_on(session: Session, employee_id: int, on: date) -> Decimal | None:
    """The accrual cap in force for this employee on `on` — what a positive
    ledger entry RECORDS. None (no clamp) when the jurisdiction has no
    mandate/ruleset for a cap, when no placement resolves, or when there is
    no day-length data. Derived only from facts as of `on`, so a re-promote
    recomputes it deterministically."""
    from usali.assignments import primary_assignment_on
    from usali.models import Property
    from usali.sick_leave_rules import UnknownSickLeaveJurisdiction

    primary = primary_assignment_on(session, employee_id, on)
    if primary is None:
        return None
    prop = session.get(Property, primary.property_id)
    if prop is None or prop.wage_jurisdiction is None:
        return None
    try:
        rules = sick_leave_rules_for(prop.wage_jurisdiction)
    except UnknownSickLeaveJurisdiction:
        return None
    if rules is None:
        return None
    d = day_length(session, employee_id, on)
    if d is None:
        return None
    return rules.accrual_cap_hours(day_length=d).quantize(_HOURS)


def accrue_for_card(
    session: Session,
    card: Timecard,
    *,
    day_hours: dict[date, Decimal],
    exempt: bool,
    jurisdiction: str | None,
) -> None:
    """Write this card's accrual entry, idempotently (delete-then-rewrite,
    keyed on timecard_id — the labor-fact pattern).

    Non-exempt staff accrue on hours WORKED, overtime included (the statute
    does not exclude it). Exempt staff are deemed 40 h per workweek of the
    card in which they were EMPLOYED (an assignment in force) — deeming on
    punched weeks under-accrued salaried managers who don't badge reliably
    (the statute review's HIGH-1); §246's deeming is not conditioned on
    time capture. Residual gap, recorded in the backlog: a period with no
    card at all still accrues nothing.

    Quantization is ROUND_UP (ceiling at 2 dp): "at least one hour per 30"
    is a floor, and half-up left a worst-case year minutes short of it.
    """
    session.execute(
        delete(SickLeaveLedger).where(
            SickLeaveLedger.timecard_id == card.timecard_id
        )
    )
    if jurisdiction is None:
        return
    rules = sick_leave_rules_for(jurisdiction)  # unknown: raises, blocks promote
    if rules is None:  # plain US: encoded absence of a mandate
        return
    if card.period_end > rules.recheck_by:
        # Encoded law goes stale. Wired to the CARD's date, not the wall
        # clock, so it is deterministic and testable.
        _LOG.warning(
            "sick-leave ruleset %s is past its re-check date (%s): re-verify "
            "%s before trusting accruals dated %s",
            rules.jurisdiction, rules.recheck_by.isoformat(),
            rules.reference_doc, card.period_end.isoformat(),
        )
    if exempt:
        from usali.assignments import assignments_on

        weeks = ((card.period_end - card.period_start).days + 1 + 6) // 7
        employed_weeks = 0
        for week in range(weeks):
            week_start = card.period_start + timedelta(days=7 * week)
            if any(
                assignments_on(session, card.employee_id,
                               week_start + timedelta(days=offset))
                for offset in range(7)
            ):
                employed_weeks += 1
        basis = rules.exempt_deemed_weekly_hours * employed_weeks
    else:
        basis = sum(day_hours.values(), Decimal("0"))
    accrued = (basis / rules.hours_worked_per_accrued_hour).quantize(
        _HOURS, rounding=ROUND_UP
    )
    if accrued <= 0:
        return
    session.add(SickLeaveLedger(
        employee_id=card.employee_id,
        entry_type="accrual",
        hours=accrued,
        cap_hours=cap_hours_on(session, card.employee_id, card.period_end),
        effective_on=card.period_end,
        timecard_id=card.timecard_id,
    ))


def _fold(
    entries: list[tuple[date, Decimal, Decimal | None]],
) -> tuple[Decimal, Decimal]:
    """(final balance, minimum end-of-day balance) over (effective_on, hours,
    cap_hours) tuples. DAY-granular: one day's entries sum before the clamp,
    so within-day order cannot change the answer; the clamp uses the day's
    own recorded caps (min of them), never a cap derived later."""
    running = Decimal("0")
    lowest = Decimal("0")
    i = 0
    entries = sorted(entries, key=lambda e: e[0])
    while i < len(entries):
        day = entries[i][0]
        day_sum = Decimal("0")
        day_caps: list[Decimal] = []
        while i < len(entries) and entries[i][0] == day:
            day_sum += Decimal(str(entries[i][1]))
            if entries[i][2] is not None:
                day_caps.append(Decimal(str(entries[i][2])))
            i += 1
        running += day_sum
        if day_caps:
            running = min(running, min(day_caps))
        lowest = min(lowest, running)
    return running, lowest


def balance_on(session: Session, employee_id: int, on: date) -> Decimal:
    """The derived balance: the day-granular fold over entries dated <= `on`,
    clamped at each day's RECORDED caps. Pure function of the ledger — no
    today's-D dependence, so the same query answers the same tomorrow."""
    entries = [
        (e.effective_on, e.hours, e.cap_hours)
        for e in session.execute(
            select(SickLeaveLedger).where(
                SickLeaveLedger.employee_id == employee_id,
                SickLeaveLedger.effective_on <= on,
            )
        ).scalars()
    ]
    final, _ = _fold(entries)
    return final.quantize(_HOURS, rounding=ROUND_HALF_UP)


def would_overdraw(
    session: Session, employee_id: int, hours: Decimal, on: date
) -> bool:
    """Would recording `hours` of usage on `on` drive the balance below zero
    on ANY day — including days AFTER `on`? A backdated usage that passes a
    point-in-time check can still overdraw a later date (money review F2),
    so the whole timeline is folded with the hypothetical entry included."""
    entries = [
        (e.effective_on, e.hours, e.cap_hours)
        for e in session.execute(
            select(SickLeaveLedger).where(
                SickLeaveLedger.employee_id == employee_id
            )
        ).scalars()
    ]
    entries.append((on, -hours, None))
    _, lowest = _fold(entries)
    return lowest < 0


@dataclass(frozen=True)
class SickDayTaken:
    """One employee-day of sick leave actually taken in a window, voids
    netted, resolved to where it belongs and what it costs. `property_id`
    None = unattributable (no or ambiguous primary placement on the day —
    each consumer decides its own response); `rate` None = unpriced (no
    dated regular rate in force, or the resolver refused)."""

    employee_id: int
    day: date
    hours: Decimal  # positive
    assignment_id: int | None
    property_id: str | None
    department_id: int | None
    rate: Decimal | None


def net_usage_by_day(
    entries: Sequence[SickLeaveLedger],
) -> dict[tuple[int, date], Decimal]:
    """Signed net of usage/usage_void rows per (employee, effective_on) —
    negative means taken. THE one copy of the netting: `sick_days_taken`
    clamps it at zero (a void erases a day, never inverts it) and the
    §246(n) late guard reads the sign to tell late-taken from late-voided.
    (G7 review: the guard had grown an inline second copy.)"""
    net: dict[tuple[int, date], Decimal] = {}
    for e in entries:
        key = (e.employee_id, e.effective_on)
        net[key] = net.get(key, Decimal("0")) + Decimal(str(e.hours))
    return net


def sick_days_taken(
    session: Session, start: date, end: date
) -> list[SickDayTaken]:
    """THE derivation of sick leave taken — ONE copy (G3), consumed by the
    SOS Schedule 14 section AND the pay-run submission. Two copies would be
    two chances for books and paychecks to disagree about the same hours.

    Usage and usage_void net per employee-day (a voided usage was never
    taken — `_used_in_year` and the cap fold already said so; the SOS
    saying otherwise was the G3 finding), clamped at zero: a void can
    erase a day, never invert it. Attribution is the primary placement on
    the day taken; pricing is the dated `regular` rate through the same
    resolver wages use. Nothing is stored — a closed window re-derives
    byte for byte."""
    from usali.assignments import AmbiguousAssignmentError, primary_assignment_on
    from usali.rates import RateError, rate_on

    entries = session.execute(
        select(SickLeaveLedger).where(
            SickLeaveLedger.entry_type.in_(("usage", "usage_void")),
            SickLeaveLedger.effective_on >= start,
            SickLeaveLedger.effective_on <= end,
        )
    ).scalars().all()
    net = net_usage_by_day(entries)
    days: list[SickDayTaken] = []
    for (employee_id, day), signed in sorted(net.items()):
        taken = -signed  # usage rows are negative; voids positive
        if taken <= 0:
            continue
        try:
            primary = primary_assignment_on(session, employee_id, day)
        except AmbiguousAssignmentError:
            primary = None
        if primary is None:
            days.append(SickDayTaken(
                employee_id=employee_id, day=day, hours=taken,
                assignment_id=None, property_id=None, department_id=None,
                rate=None,
            ))
            continue
        try:
            rate = rate_on(session, primary.assignment_id, "regular", day)
        except RateError:
            rate = None
        days.append(SickDayTaken(
            employee_id=employee_id, day=day, hours=taken,
            assignment_id=primary.assignment_id,
            property_id=primary.property_id,
            department_id=primary.department_id,
            rate=rate,
        ))
    return days
