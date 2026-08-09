"""Promote approved timecards into estimated labor facts (Pillar B3).

Reads each approved timecard's per-day hours (B2's engine) and the ENCRYPTED
rates in force server-side, runs the overtime engine, and writes
DEPARTMENT-LEVEL aggregates to `usali_labor_fact`. Individual rates never leave
this function — only summed cost is stored. `est_cost` is an ESTIMATE.

Since E2 a rate belongs to a PLACEMENT and a DATE RANGE, so it is resolved per
property and per business date inside the loop. That is what makes a re-promote
of a closed period reproduce it byte for byte after a later raise — the
invariant `tests/test_closed_period_stability.py` pins.

Idempotent: a re-promote deletes this timecard's existing labor facts and
re-inserts, so re-running (or the CLI backfill) never double-counts.
"""

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from usali.models import EXCLUDE_FROM_PAYROLL, Employee, Property, Timecard, UsaliLaborFact
from usali.apportion import apportion
from usali.assignments import (
    assignment_at,
    primary_assignment_on,
    resolve_exemption,
)
from usali.attribution import property_minutes_for_day
from usali.overtime import compute_overtime
from usali.overtime_rules import OvertimeRules, rules_for
from usali.rates import HourlyRates, hourly_rates_on
from usali.sick_leave import accrue_for_card
from usali.timecards import compute_timecard

_LOG = logging.getLogger(__name__)
_CENTS_PER_HOUR = Decimal("0.01")
_MONEY = Decimal("0.0001")


def promote_timecard(session: Session, card: Timecard, *, anchor: date) -> int:
    """Promote one APPROVED timecard to `usali_labor_fact` rows (one per worked
    business date). Returns the number of rows written. Raises ValueError if the
    card is not approved — only approved hours are promoted."""
    if card.status != "approved":
        raise ValueError(f"timecard {card.timecard_id} is not approved (status={card.status})")

    employee = session.get(Employee, card.employee_id)
    assert employee is not None
    if employee.pay_type == EXCLUDE_FROM_PAYROLL:
        # NO facts at all — not zero-cost rows, which would put an owner's
        # hours into Schedule 15 and the denominator of every hours statistic
        # (E3 decision 4).
        #
        # But if this card ALREADY carries facts, they were promoted while the
        # person was NOT excluded — deleting them here would silently restate
        # a filed Schedule 14 to zero on a routine re-promote, the exact harm
        # `terminate_employee` refuses one module over (its stranded-day
        # guard). The E3 review reproduced it: $160 filed, pay_type flipped,
        # backfill re-run, $160 gone with only a log line. Refuse instead;
        # resolving a real misclassification means correcting the filed facts
        # deliberately, not having idempotency eat them. (ValueError is the
        # family promote callers already handle with a clean rollback.)
        filed = session.execute(
            select(func.count()).select_from(UsaliLaborFact).where(
                UsaliLaborFact.timecard_id == card.timecard_id
            )
        ).scalar_one()
        if filed:
            raise ValueError(
                f"employee {employee.employee_id} is exclude_from_payroll but "
                f"timecard {card.timecard_id} already carries {filed} filed "
                "labor fact(s) promoted under a payable classification. "
                "Re-promoting would silently restate that filed cost to zero. "
                "Correct the classification or the filed facts deliberately."
            )
        _LOG.info(
            "employee_id=%s is exclude_from_payroll; timecard %s promoted no facts",
            employee.employee_id, card.timecard_id,
        )
        return 0
    # Per-day worked hours from B2's engine (lunch already excluded), plus the
    # property split for each day. Computed BEFORE exemption because exemption is
    # now resolved over the days actually worked, not sampled at period_start.
    days = [d for d in compute_timecard(session, card) if d.worked_minutes > 0]
    day_hours = {
        d.business_date: (Decimal(d.worked_minutes) / Decimal("60")).quantize(_CENTS_PER_HOUR)
        for d in days
    }
    day_split = {
        d.business_date: property_minutes_for_day(
            session,
            employee_id=card.employee_id,
            business_date=d.business_date,
            property_minutes=d.property_minutes,
        )
        for d in days
    }

    # ONE answer for the card, or a refusal. The previous shape sampled
    # period_start here and "corrected" it per day further down -- but this
    # sample gated whether `rate` was read AT ALL, so the per-day correction
    # operated on a rate already set to None and could only turn pay off, never
    # on. A mutation collapsing that correction back to a single sample passed
    # all 913 tests, which is how it stayed hidden.
    exempt = resolve_exemption(session, card.employee_id, day_hours)
    if exempt:
        # The estimate prices HOURLY labor; exempt (salaried) staff must never
        # be costed hours×rate — a salary is not a wage. Hours still promote
        # (Schedule 15 stays complete); Pillar C's gross-to-net prices salaries.
        _LOG.warning(
            "employee_id=%s is FLSA-exempt; labor hours promoted with est_cost=0",
            employee.employee_id,
        )

    # Re-promote safety: clear this timecard's prior facts first.
    session.execute(delete(UsaliLaborFact).where(UsaliLaborFact.timecard_id == card.timecard_id))

    written = 0
    # ORDER IS LOAD-BEARING: overtime runs on the employee's COMBINED hours
    # first, and only the resulting hours are split across properties. Splitting
    # first and running overtime per property would turn 6h at one hotel plus 5h
    # at the other into two sub-8-hour days and compute ZERO daily overtime on an
    # 11-hour day -- an underpayment, not a reporting error. California overtime
    # is per-employer, not per-work-location (Lab. Code 500/510(a), Wage Order 5;
    # see docs/reference/overtime-jurisdictions.md).
    for row in compute_overtime(
        day_hours,
        anchor=anchor,
        exempt=exempt,
        rules=_rules_for_card(session, employee, day_split, card.period_start),
    ):
        split = day_split[row.business_date]
        regular = apportion(row.regular_hours, split, quantum=_CENTS_PER_HOUR)
        overtime_h = apportion(row.ot_hours, split, quantum=_CENTS_PER_HOUR)
        doubletime = apportion(row.dt_hours, split, quantum=_CENTS_PER_HOUR)

        for property_id in split:
            reg = regular[property_id]
            ot = overtime_h[property_id]
            dt = doubletime[property_id]
            if reg + ot + dt <= 0:
                continue

            # PER PROPERTY, PER BUSINESS DATE — never once per card. Both axes
            # are load-bearing since E2 and each has already produced a shipped
            # bug in its own right:
            #
            #   per property: the rate hangs off the PLACEMENT, so front desk
            #     at one hotel and laundry at the other are genuinely different
            #     money. One rate for the card prices half the hours wrong.
            #   per date: this is the exemption bug's exact shape, twice over —
            #     a value sampled once and applied to all fourteen days. A raise
            #     mid-period is ordinary, and sampling would pay every day at
            #     whichever side of it the sample landed.
            assignment = assignment_at(
                session, card.employee_id, property_id, row.business_date
            )
            rates = (
                None
                if exempt or assignment is None
                else hourly_rates_on(
                    session, assignment.assignment_id, row.business_date
                )
            )
            if rates is None and not exempt:
                _LOG.warning(
                    "no rate in effect for employee_id=%s at property_id=%s on %s; "
                    "labor hours promoted with est_cost=0",
                    card.employee_id, property_id, row.business_date.isoformat(),
                )
            cost = Decimal("0") if rates is None else _price(reg, ot, dt, rates)

            session.add(UsaliLaborFact(
                property_id=property_id,
                business_date=row.business_date,
                department_id=department_at(
                    session, card.employee_id, property_id, row.business_date
                ),
                hours=(reg + ot + dt).quantize(_CENTS_PER_HOUR),
                ot_hours=(ot + dt).quantize(_CENTS_PER_HOUR),
                est_cost=cost.quantize(_MONEY, rounding=ROUND_HALF_UP),
                timecard_id=card.timecard_id,
            ))
            written += 1
    # E4: sick leave accrues off the same approved hours, in the same
    # idempotent pass (its delete-then-rewrite keys on this card, like the
    # facts above). Excluded staff never reach here (the skip returned 0),
    # matching their no-facts treatment.
    accrue_for_card(
        session, card, day_hours=day_hours, exempt=exempt,
        jurisdiction=_jurisdiction_for_card(
            session, employee, day_split, card.period_start
        ),
    )
    session.flush()
    return written


def demote_timecard(session: Session, card: Timecard) -> int:
    """Delete this card's ESTIMATED labor facts (H3 reopen) — the exact
    inverse of the promote's delete-then-rewrite, keyed the same way. A
    reopened card's hours are under review again and must not keep
    claiming approved cost on any report; re-approval re-promotes them.
    Actual (pay-run) facts are untouched — paid history is immutable.
    Returns the number of fact rows deleted."""
    result = cast("CursorResult[Any]", session.execute(
        delete(UsaliLaborFact).where(
            UsaliLaborFact.timecard_id == card.timecard_id
        )
    ))
    return result.rowcount


def _price(reg: Decimal, ot: Decimal, dt: Decimal, rates: HourlyRates) -> Decimal:
    """Hours x the rates that actually govern them.

    `dot` is None only where the jurisdiction has no double-time rule, and there
    the engine cannot emit double-time hours — so the two conditions below are
    mutually exclusive by construction. Named rather than assumed: if a
    jurisdiction is ever encoded with a DT threshold and no multiplier, this
    refuses instead of silently pricing double time at straight time.
    """
    if dt > 0 and rates.dot is None:
        raise ValueError(
            "double-time hours were computed for a jurisdiction with no "
            "double-time rate; refusing to price them"
        )
    dt_cost = Decimal("0") if rates.dot is None else dt * rates.dot
    return reg * rates.regular + ot * rates.ot + dt_cost


def department_at(
    session: Session, employee_id: int, property_id: str, on: date
) -> int | None:
    """The department this employee's assignment at THIS property points to.

    Department is a per-assignment attribute now: the same person can be
    Housekeeping at one hotel and Laundry at the other, so a single employee-wide
    department would mis-file half their hours on Schedule 14.

    Goes through `assignment_at` rather than scanning placements itself, because
    since E2 the RATE hangs off the same placement. Two lookups could disagree
    about which one a day's hours belong to, and the hours would then file under
    one department at the rate of another.
    """
    assignment = assignment_at(session, employee_id, property_id, on)
    if assignment is not None:
        return assignment.department_id

    # No assignment at this property on this date. Attribution is kiosk-derived
    # and therefore WIDER than assignment, so this is reachable: a punch at a
    # property the person has since transferred away from. The hours are real
    # and must not be dropped, but they belong to no department -- the
    # Unassigned bucket is visible and obviously wrong, which is what we want.
    _LOG.warning(
        "employee_id=%s has no assignment at property_id=%s on %s; "
        "labor filed under no department",
        employee_id, property_id, on.isoformat(),
    )
    return None


def _rules_for_card(
    session: Session,
    employee: Employee,
    day_split: dict[date, dict[str, int]],
    card_start: date,
) -> OvertimeRules:
    """The overtime ruleset for this card.

    Overtime is computed on combined hours, so ONE ruleset governs the whole
    card. That is only coherent while every property worked shares a
    jurisdiction. If a card ever spans two, refuse: which state's daily rule
    governs a day split across state lines is a genuine legal question this
    engine has no answer for, and picking one silently would misstate wages.
    """
    return rules_for(
        _jurisdiction_for_card(session, employee, day_split, card_start)
    )


def _jurisdiction_for_card(
    session: Session,
    employee: Employee,
    day_split: dict[date, dict[str, int]],
    card_start: date,
) -> str | None:
    """The ONE wage jurisdiction governing this card, shared by overtime
    rules and sick-leave accrual — extracted so the two consumers cannot
    disagree about which state's law a card falls under (E4; the one-
    predicate rule). May return None (a property with a NULL jurisdiction),
    which every consumer must refuse rather than default."""
    # From the RAW per-day property sets, not the post-rounding split. _apportion
    # drops zero shares, so a property contributing real but sub-cent time
    # vanished from this check -- letting a card silently cost entirely under the
    # other property's rules. Reproduced by review with 1 minute at a US-FLSA
    # property alongside 8h in California.
    worked = {p for split in day_split.values() for p in split}
    jurisdictions: dict[str, str | None] = {}
    for property_id in sorted(worked):
        prop = session.get(Property, property_id)
        if prop is None:
            raise ValueError(f"unknown property {property_id} on timecard")
        # A NULL jurisdiction stays NULL and reaches rules_for, which refuses.
        # Substituting anything here would reintroduce the silent default this
        # whole change exists to remove.
        jurisdictions[property_id] = prop.wage_jurisdiction
    distinct = set(jurisdictions.values())
    if len(distinct) > 1:
        raise ValueError(
            f"employee {employee.employee_id} worked across multiple wage "
            f"jurisdictions on one timecard ({jurisdictions}). Overtime is "
            "computed on combined hours, so one ruleset must govern the card, "
            "and which state's daily rule applies to a day split across state "
            "lines is not settled here. Refusing to pick one."
        )
    if not distinct:
        # No worked property at all (an empty card). Fall back to the primary
        # assignment's jurisdiction; if there is none there is nothing to cost.
        primary = primary_assignment_on(session, employee.employee_id, card_start)
        if primary is None:
            return "US"
        prop = session.get(Property, primary.property_id)
        if prop is None:
            raise ValueError(f"unknown property {primary.property_id}")
        return prop.wage_jurisdiction
    return next(iter(distinct))


