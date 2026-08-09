"""Effective-dated rate resolution — THE ONLY place this logic lives.

The counterpart to `usali.assignments`, and it exists for the same reason. A
rate belongs to a PLACEMENT and a DATE RANGE, not to a person: the same employee
can earn one rate at the front desk and another in laundry, and both change over
time. `Employee.pay_rate` was a single undated scalar, so re-promoting a closed
period after a raise re-costed already-worked hours — an April period costing
$160 became $240 because a raise dated 1 August was entered, and a filed
Schedule 14 stopped tying to what was filed.

Dating is **[effective_from, effective_to)**, via `assignments.covers` rather
than a second comparison written here.

## `None` means "cannot cost". It never means zero.

No rate history was imported, so any date before the payroll anchor resolves to
nothing, as does a rate type that was never stored. Callers must handle `None`
exactly as they handle `primary_assignment_on` returning `None`. Substituting
zero would be worse than a crash: zero is a PRICED value, so it enters the
population every suppression gate counts, and a department whose only employee
cannot be costed would publish a real-looking $0 instead of suppressing.

## The statutory floor

All four rate types are stored, including `ot` and `dot` which are computable
today as `regular x 1.5` and `regular x 2`. That derivability is a coincidence
of the two jurisdictions encoded so far, not a property of the domain, and a
stored premium is also how a union or contract rate is expressed.

Storing them creates a conflict that could not exist before: **a stored premium
can disagree with the jurisdiction multiplier.** The rule is that the stored
rate governs, but never below the floor. Above is lawful and is the entire point
of the feature. Below is a data error, and it is REFUSED rather than silently
lifted to the floor (which pays a figure no record shows) or silently honoured
(which underpays against a rule we hold a citation for).

Checked here, at COSTING time, as well as at entry — defence in depth, because
the entry path is not the only writer. A migration, a backfill, or a fix applied
straight to the database all reach the costing path and none reach validation.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.assignments import covers
from usali.models import AssignmentRate, EmployeeAssignment, Property
from usali.overtime_rules import OvertimeRules, rules_for

# The premium types, and the `OvertimeRules` attribute holding each one's floor
# multiplier. `holiday` is deliberately absent: nothing in the engine has a
# concept of a holiday, so there is no statutory figure to measure it against,
# and inventing one would refuse data that breaks no rule.
_FLOOR_MULTIPLIER_ATTR = {"ot": "ot_multiplier", "dot": "dt_multiplier"}

RATE_TYPES = frozenset({"regular", "ot", "dot", "holiday"})


class RateError(Exception):
    """Base for every refusal this module makes.

    Exists so a caller can decline the whole class in one clause. Preflight is
    the reason: its entire job is to name blockers a Payroll Admin can fix
    BEFORE any provider call, so a rate problem must arrive as a named blocker
    on that employee's line, never as an unhandled 500 that tells nobody which
    employee to look at and blocks the pay run for everyone.
    """


class AmbiguousRateError(RateError):
    """More than one rate of the same type is in force on the same date.

    Raised rather than resolved, exactly like `AmbiguousPrimaryError`. Two
    answers to "what does this person earn" is a data error, and breaking the
    tie prices real worked hours at a figure nobody chose — silently, since
    either candidate produces a plausible number. A caller cannot recover from
    this; the data needs fixing.
    """


class BelowStatutoryFloorError(RateError):
    """A stored premium rate falls below the jurisdiction's statutory minimum.

    Refused rather than lifted or honoured — see the module docstring. The
    message names the stored rate, the floor, and the jurisdiction, because the
    person who has to fix it needs to know which of the three is wrong.
    """


class MissingBaseRateError(RateError):
    """A premium rate is stored with no `regular` rate in effect to measure it
    against, so its lawfulness cannot be established.

    Fail closed. Honouring an unverifiable premium pays money the system cannot
    show is lawful; treating it as absent would fall back to `regular x
    multiplier` off a base rate that is also missing. The same stance as
    `is_exempt_on` treating silence as non-exempt.
    """


class CorruptRateError(RateError, ValueError):
    """A stored rate did not decrypt to a decimal.

    Named rather than surfaced as `InvalidOperation` from deep inside arithmetic:
    a bare `InvalidOperation` reaching a caller that catches `ValueError` is
    invisible, which is exactly the B3 low that E1 cleared for `pay_rate`. The
    message names the employee and the assignment — never the ciphertext, never
    the value.

    **Subclasses ValueError as well as RateError deliberately**, and that is a
    contract rather than a convenience: `promote_timecard` documented its corrupt-rate failure as "the
    ValueError callers already handle (clean rollback)", and callers were written
    against it. Making this a bare Exception would have walked that back to the
    exact 500-with-no-rollback the E1 chip closed.
    """


def rate_on(
    session: Session, assignment_id: int, rate_type: str, on: date
) -> Decimal | None:
    """What this placement pays per hour for `rate_type` on `on`, or `None`.

    `None` means no rate is in effect — a date before the anchor, a rate type
    never stored, or a closed range. Callers MUST treat it as "cannot cost" and
    never as zero.
    """
    if rate_type not in RATE_TYPES:
        raise ValueError(
            f"unknown rate type {rate_type!r}; known: {', '.join(sorted(RATE_TYPES))}"
        )

    amount = _stored_rate(session, assignment_id, rate_type, on)
    if amount is None:
        return None

    floor_attr = _FLOOR_MULTIPLIER_ATTR.get(rate_type)
    if floor_attr is not None:
        _refuse_below_floor(session, assignment_id, rate_type, on, amount, floor_attr)
    return amount


@dataclass(frozen=True)
class HourlyRates:
    """Every rate needed to price one day's hours at one placement.

    `dot` is `None` where the jurisdiction has no double-time rule. The engine
    can never emit double-time hours there, so there is no rate to invent, and
    `None` beats a fabricated 2x that would quietly become real money if a
    jurisdiction were ever mis-encoded.
    """

    regular: Decimal
    ot: Decimal
    dot: Decimal | None


def hourly_rates_on(
    session: Session, assignment_id: int, on: date
) -> HourlyRates | None:
    """THE costing view of a placement's pay on `on`, or `None` if it cannot be
    priced at all.

    A stored premium governs; an absent one falls back to `regular x` the
    jurisdiction multiplier, which is what the engine has always done and what
    the backfill deliberately relies on by not populating `ot`/`dot`.

    The fallback lives HERE rather than at each consumer because it had two
    module-level copies before this — `labor.py` and `schedule_api.py` each held
    their own `1.5` and `2` — and two copies of a rule are two chances to drift
    from the citation. Four consumers would have been four.
    """
    regular = rate_on(session, assignment_id, "regular", on)
    if regular is None:
        # Not a zeroed rate set. Callers promote the HOURS with no cost, which
        # keeps Schedule 15 complete while keeping this employee out of the
        # priced population every suppression gate counts.
        return None

    rules = _rules_for_assignment(session, assignment_id)
    stored_ot = rate_on(session, assignment_id, "ot", on)
    stored_dot = rate_on(session, assignment_id, "dot", on)
    # `dot` is None wherever the jurisdiction has NO double-time rule, and that
    # holds even against a STORED dot rate. The engine cannot emit double-time
    # hours there (`overtime.py` needs a daily_dt threshold), so there is no hour
    # for the rate to price -- and returning a stored value would break the
    # contract `HourlyRates.dot` states, letting a future consumer price double
    # time at a figure the jurisdiction never authorises. A stored dot under a
    # no-DT jurisdiction is a data error the pay-run premium block already
    # refuses; here it simply resolves to None.
    if rules.dt_multiplier is None:
        dot: Decimal | None = None
    elif stored_dot is not None:
        dot = stored_dot
    else:
        dot = regular * rules.dt_multiplier
    return HourlyRates(
        regular=regular,
        ot=stored_ot if stored_ot is not None else regular * rules.ot_multiplier,
        dot=dot,
    )


def _stored_rate(
    session: Session, assignment_id: int, rate_type: str, on: date
) -> Decimal | None:
    """The one rate row of this type in force on `on`, decoded, or `None`.

    The date filter is applied in PYTHON via `covers`, not in SQL. `amount` is
    encrypted so the database cannot do arithmetic on it anyway, and more to the
    point a WHERE clause here would be a second copy of the dating rule, free to
    drift from `covers` — which is the duplication E1 spent three reviews
    unwinding. SQL narrows to the rows that could match; `covers` decides.
    """
    rows = [
        row
        for row in session.execute(
            select(AssignmentRate).where(
                AssignmentRate.assignment_id == assignment_id,
                AssignmentRate.rate_type == rate_type,
                AssignmentRate.effective_from <= on,
            )
        ).scalars()
        if covers(row.effective_from, row.effective_to, on)
    ]
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousRateError(
            f"assignment {assignment_id} has {len(rows)} {rate_type} rates in "
            f"force on {on.isoformat()}. Refusing to choose: either would price "
            "real worked hours at a figure nobody chose, and both produce a "
            "plausible number. Close the superseded rate."
        )
    return _decode(session, rows[0], assignment_id, rate_type)


def _decode(
    session: Session, row: AssignmentRate, assignment_id: int, rate_type: str
) -> Decimal:
    try:
        return Decimal(row.amount)
    except InvalidOperation:
        # Names the EMPLOYEE, not only the assignment: whoever has to act on
        # this needs to know who is unpayable, and an assignment id does not say
        # that. Never the value itself -- it is compensation data, and this
        # message reaches logs.
        assignment = session.get(EmployeeAssignment, assignment_id)
        who = (
            f"employee {assignment.employee_id}" if assignment is not None
            else "an unknown employee"
        )
        raise CorruptRateError(
            f"the stored {rate_type} rate for {who} (assignment {assignment_id}) "
            "did not decrypt to a decimal. This is a key or data-integrity "
            "problem, not a costing one; hours cannot be priced until it is "
            "resolved."
        ) from None


def _refuse_below_floor(
    session: Session,
    assignment_id: int,
    rate_type: str,
    on: date,
    amount: Decimal,
    floor_attr: str,
) -> None:
    rules = _rules_for_assignment(session, assignment_id)
    multiplier: Decimal | None = getattr(rules, floor_attr)
    if multiplier is None:
        # The jurisdiction has no such premium rule, so there is no statutory
        # figure to fall below — double time under the FLSA baseline, for
        # instance. Applying California's 2x here would refuse lawful data in
        # every state but one.
        return

    # The floor is read on the date being COSTED, from the `regular` rate in
    # force then. Reading the latest one instead would refuse an OT rate that
    # was lawful in April because a raise in August moved the floor above it.
    base = _stored_rate(session, assignment_id, "regular", on)
    if base is None:
        raise MissingBaseRateError(
            f"assignment {assignment_id} has a stored {rate_type} rate but no "
            f"regular rate in force on {on.isoformat()}, so there is nothing to "
            f"check the {rules.jurisdiction} statutory floor against. Refusing "
            "to price hours at a premium whose lawfulness cannot be established."
        )

    floor = base * multiplier
    if amount < floor:
        # NAME NO FIGURE. `amount`, `base` and `floor` are all compensation --
        # `floor` is `base x multiplier`, so it discloses the regular rate too.
        # This message reaches request logs (the pay-run 422 detail) and, before
        # the schedule projection's `except` was widened, an unhandled traceback.
        # `_decode` above already omits its value for exactly this reason; this
        # follows suit. The payroll admin identifies the bad record from the
        # employee + rate type + date and reads the value through the audited
        # `GET /pay-rate`, which is the one sanctioned disclosure path.
        raise BelowStatutoryFloorError(
            f"assignment {assignment_id} has a stored {rate_type} rate on "
            f"{on.isoformat()} below the {rules.jurisdiction} statutory floor "
            f"(the regular rate x its overtime multiplier). Refusing: lifting it "
            "silently would pay a figure no record shows, and honouring it would "
            f"underpay against {rules.citation}. Correct the stored rate."
        )


def _rules_for_assignment(session: Session, assignment_id: int) -> OvertimeRules:
    """The wage ruleset governing this placement's property.

    Resolved ONLY for premium types. A `regular` rate must stay resolvable at a
    property whose jurisdiction has not been stated yet — that is a legitimate
    pre-payroll state (a property can be created by PMS ingestion, which knows
    nothing about wage law), and failing there would block onboarding on a fact
    only needed to check a premium nobody has stored.
    """
    assignment = session.get(EmployeeAssignment, assignment_id)
    if assignment is None:
        raise ValueError(f"no assignment {assignment_id}")
    prop = session.get(Property, assignment.property_id)
    return rules_for(prop.wage_jurisdiction if prop is not None else None)
