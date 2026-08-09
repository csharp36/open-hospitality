"""Effective-dated assignment resolution — THE ONLY place this logic lives.

Dating is **[effective_from, effective_to)** — inclusive start, EXCLUSIVE end.
A transfer dated 2026-03-01 gives that day entirely to the new assignment and
none of it to the old.

Every caller goes through here. Hand-rolled date comparisons at call sites are
how off-by-one attribution bugs get in, and an off-by-one here silently moves a
full day of labor cost to the wrong property's Schedule 14 — the arithmetic
succeeds, so nothing downstream signals it.
"""

from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import EmployeeAssignment, Position


def has_assignment_rows(session: Session, employee_id: int) -> bool:
    """Does this employee have ANY assignment rows, regardless of date or status?

    THE RAMP GATE. Every migration ramp must consult this FIRST, before asking
    what is effective. "No rows effective on this date" and "no rows at all" are
    completely different states:

      - no rows at all      -> pre-E1 data; the retiring column is all we have
      - rows, none effective -> the assignments have a DEFINITE opinion, namely
                                that this person is not employed/assigned here
                                right now (ended, deactivated, or not yet started)

    Treating the second as the first lets the retiring column overrule the
    assignment table and readmit people it has deliberately excluded. That
    already shipped once as a real bug -- a terminated employee still admitted
    at a kiosk -- and an adversarial review then found the same mistake in five
    more ramps, because the reasoning lived in one function's comment instead of
    in a function.
    """
    return (
        session.execute(
            select(EmployeeAssignment.assignment_id)
            .where(EmployeeAssignment.employee_id == employee_id)
            .limit(1)
        ).first()
        is not None
    )


class AmbiguousPrimaryError(Exception):
    """More than one assignment is marked primary on the same date.

    Raised rather than picking one. The primary assignment decides which
    property's pay run issues the paycheck; resolving a tie arbitrarily is
    exactly how one timecard reaches two pay runs and the employee is paid
    twice. A caller cannot recover from this sensibly — the data needs fixing.
    """


def covers(effective_from: date, effective_to: date | None, on: date) -> bool:
    """Does the half-open range **[effective_from, effective_to)** contain `on`?

    THE date arithmetic — ONE copy, shared by every effective-dated row in the
    system. `in_effect_on` adds the status rule on top of this for assignments;
    `usali.rates` uses it bare, because a rate has no status to reconcile.

    It exists as its own function so a second dated table cannot arrive with a
    second, subtly different comparison. An off-by-one here moves a full day of
    labor cost to the wrong Schedule 14 — or, on a rate, prices already-worked
    hours at a raise that had not happened yet. Both succeed arithmetically, so
    nothing downstream signals them.
    """
    if effective_from > on:
        return False
    # Exclusive end: a range ending on `on` is already over. A transfer or a
    # raise dated the 1st gives that day entirely to the new row.
    if effective_to is not None:
        return effective_to > on
    return True


def in_effect_on(assignment: EmployeeAssignment, on: date) -> bool:
    """Was this assignment in force on `on`? THE date predicate — ONE copy.

    Status and effective dating answer DIFFERENT questions, and conflating them
    stranded a terminated employee's final paycheck. `terminate_employee` sets
    `effective_to = last_day + 1` (exclusive, so the last worked day stays
    inside) AND `status = "inactive"`; every population then filtered on
    `status == "active"`, which made the careful dating irrelevant on every
    date, including days already worked. Approved hours went unsubmitted with
    no error naming the employee.

    So: **once an assignment carries an end date, dating ALONE governs.** The
    end date is the considered answer to "until when", and a status that
    contradicts it is not a second opinion worth honouring.

    `status == "inactive"` with NO end date is a different thing — someone
    deactivated without anyone saying when. That has no defensible date answer,
    so it is in force on no date. Failing closed is right for a predicate that
    both opens doors and issues paychecks.
    """
    if not covers(assignment.effective_from, assignment.effective_to, on):
        return False
    # Dating alone governs once an end date exists; only an OPEN assignment is
    # left for status to decide.
    return assignment.effective_to is not None or assignment.status == "active"


def overlaps_period(assignment: EmployeeAssignment, start: date, end: date) -> bool:
    """Was this assignment in force on ANY day of [start, end], inclusive?

    A pay period is a RANGE, and asking a point-in-time question about it is how
    the stranded-paycheck bug survived: someone terminated on the 10th holds no
    assignment on the 19th, so a population sampled at `period_end` omits them
    and the days they actually worked are never submitted.
    """
    if assignment.effective_from > end:
        return False
    if assignment.effective_to is not None and assignment.effective_to <= start:
        return False
    return assignment.effective_to is not None or assignment.status == "active"


def assignments_on(
    session: Session, employee_id: int, on: date, *, active_only: bool = True
) -> list[EmployeeAssignment]:
    """Every assignment in effect for this employee on `on`.

    `active_only=False` includes inactive rows — for audit and history views.
    Costing and authorization must use the default: a terminated person still
    has a row, and picking it up would price hours for someone who has left.
    """
    stmt = select(EmployeeAssignment).where(
        EmployeeAssignment.employee_id == employee_id,
        EmployeeAssignment.effective_from <= on,
    )
    rows = list(session.execute(stmt).scalars())
    if active_only:
        # `in_effect_on` owns the status-vs-dating rule. Do not re-derive it here
        # -- re-deriving it at each call site is precisely what stranded the
        # final paycheck.
        return sorted(
            (a for a in rows if in_effect_on(a, on)), key=lambda a: a.property_id
        )
    # History view: dating only, so a terminated row stays visible on the dates
    # it actually covered.
    return sorted(
        (a for a in rows if a.effective_to is None or a.effective_to > on),
        key=lambda a: a.property_id,
    )


class AmbiguousAssignmentError(Exception):
    """One employee holds two placements at the SAME property on one date.

    Refused rather than resolved. Before E2 this choice only picked a
    department and `department_at` quietly took the first match; since E2 the
    same choice selects a RATE, so taking either arbitrarily prices real worked
    hours at a figure nobody chose — silently, because both produce a plausible
    number. Same stance, and the same reason, as `AmbiguousRateError`.
    """


def assignment_at(
    session: Session, employee_id: int, property_id: str, on: date
) -> EmployeeAssignment | None:
    """This employee's placement at THIS property on `on`, or None.

    ONE lookup shared by everything that hangs off a placement — department
    attribution and, since E2, rate resolution. Two copies could disagree about
    which placement a day's hours belong to, and then the hours would file under
    one department at the rate of another.

    `None` is reachable and is not an error: attribution is kiosk-derived and
    therefore WIDER than assignment, so a punch can land at a property someone
    has since transferred away from. Those hours are real and must not be
    dropped.
    """
    here = [
        a for a in assignments_on(session, employee_id, on)
        if a.property_id == property_id
    ]
    if not here:
        return None
    if len(here) > 1:
        raise AmbiguousAssignmentError(
            f"employee {employee_id} holds {len(here)} placements at "
            f"{property_id} on {on.isoformat()}. Refusing to choose: the "
            "placement decides both the department the hours file under and the "
            "rate they are priced at, so either choice invents a number. Close "
            "the superseded placement."
        )
    return here[0]


def primary_assignment_on(
    session: Session, employee_id: int, on: date
) -> EmployeeAssignment | None:
    """The paycheck-issuing assignment, or None if the employee has none in
    effect. Callers MUST handle None — a pay run cannot silently pick an
    arbitrary property for someone with no effective assignment."""
    primaries = [a for a in assignments_on(session, employee_id, on) if a.is_primary]
    if not primaries:
        return None
    if len(primaries) > 1:
        raise AmbiguousPrimaryError(
            f"employee {employee_id} has {len(primaries)} primary assignments on "
            f"{on.isoformat()} ({', '.join(a.property_id for a in primaries)}). "
            "Refusing to choose: the primary decides which property's pay run "
            "issues the paycheck, so a tie would let one timecard reach two pay "
            "runs and pay the employee twice. Fix the assignment data."
        )
    return primaries[0]


def employee_ids_with_primary_at(
    session: Session, property_id: str, on: date
) -> set[int]:
    """Employees whose PAYCHECK is issued by this property on `on`.

    This is the pay-run population. It is keyed on the primary assignment, not
    on "works here at all" — an employee assigned to two properties would
    otherwise match both properties' pay runs for the same period and be paid
    twice.

    Lives here rather than as a JOIN at the call site so the effective-dating
    comparison stays in one module.
    """
    rows = session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.property_id == property_id,
            EmployeeAssignment.is_primary.is_(True),
            EmployeeAssignment.effective_from <= on,
        )
    ).scalars()
    paid = {a.employee_id for a in rows if in_effect_on(a, on)}

    # THE DOUBLE-PAYMENT GUARANTEE, actually enforced. primary_assignment_on
    # raises on two primaries, but it is not on this path -- and this is the path
    # the pay run uses. Without the check, an employee holding a primary at BOTH
    # properties matches BOTH populations and is submitted to the provider twice.
    # A DB partial unique index makes the state unreachable; this is the
    # belt-and-braces read-side check, and it must run BEFORE any provider call.
    for employee_id in paid:
        primary_assignment_on(session, employee_id, on)

    return paid


def employee_ids_paid_by_during(
    session: Session, property_id: str, start: date, end: date
) -> set[int]:
    """Employees whose paycheck THIS property issues for the period [start, end].

    THE pay-run population. `employee_ids_with_primary_at` samples a single date
    and is wrong for this job: someone terminated on the 10th of a period ending
    the 19th holds no assignment on the 19th, so they silently drop out and the
    days they worked are never submitted to the provider.

    **Which property owns the paycheck when the primary MOVED mid-period:** the
    property holding the primary on the LAST day it was in force within the
    period. That rule is single-valued in both directions, which is what fixes
    the dropped paycheck WITHOUT reintroducing the double-payment bug:

      - terminated on the 10th -> the last in-force day is the 10th, so the
        property they left still pays for the days they worked. The bug.
      - transferred to the other hotel on the 10th -> the last in-force day is
        the 19th, so the NEW property pays, once. A bare overlap predicate would
        have put them in BOTH runs and paid them twice.

    The rule is computed by _owner_on_last_covered_day, literally, rather than
    approximated by latest-start -- see its docstring for why that matters.

    A single timecard therefore still reaches exactly one pay run.
    """
    # ONLY employees this property could plausibly owe. Resolving every employee
    # in the estate meant one unrelated person's bad assignment data raised
    # before the owner test ran, halting payroll at properties with no
    # connection to the ambiguity -- a refusal landing on people who could not
    # act on it. Narrow first, then resolve.
    mine = [
        a
        for a in session.execute(
            select(EmployeeAssignment).where(
                EmployeeAssignment.property_id == property_id,
                EmployeeAssignment.is_primary.is_(True),
                EmployeeAssignment.effective_from <= end,
            )
        ).scalars()
        if overlaps_period(a, start, end)
    ]
    if not mine:
        return set()

    all_primaries: dict[int, list[EmployeeAssignment]] = {}
    for a in session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id.in_({a.employee_id for a in mine}),
            EmployeeAssignment.is_primary.is_(True),
            EmployeeAssignment.effective_from <= end,
        )
    ).scalars():
        all_primaries.setdefault(a.employee_id, []).append(a)

    paid: set[int] = set()
    for employee_id, assignments in all_primaries.items():
        if _owner_on_last_covered_day(
            assignments, start, end, employee_id
        ) == property_id:
            paid.add(employee_id)
    return paid


def _owner_on_last_covered_day(
    assignments: list[EmployeeAssignment], start: date, end: date, employee_id: int
) -> str | None:
    """Walk back from `end` and return the property whose primary was actually
    in force on the latest covered day, or None if none was.

    This used to be `max(effective_from)`, which is NOT the same rule and the
    docstring above it said so while the code did something else. The two agree
    only when the latest-STARTING assignment is also the longest-SURVIVING one.
    They diverge on an ordinary shape: an open primary at SJCES since January
    plus a short 58033 stint from the 8th to the 11th. On the 19th the employee
    is unambiguously SJCES, but the latest start is the 8th -- so 58033 paid,
    under 58033's overtime rules, booked to 58033's P&L, and SJCES (where they
    worked 12 of 14 days) dropped them entirely. That is the stranded paycheck
    this whole function exists to prevent, reintroduced through another door.

    Walking the days is O(period length) -- fourteen iterations -- and it
    computes the sentence rather than approximating it.
    """
    on = end
    while on >= start:
        in_force = [a for a in assignments if in_effect_on(a, on)]
        owners = {a.property_id for a in in_force}
        if len(owners) > 1:
            raise AmbiguousPrimaryError(
                f"employee {employee_id} has primary assignments at "
                f"{', '.join(sorted(owners))} both in force on {on.isoformat()}. "
                "Refusing to choose which property issues the paycheck: a tie "
                "would let one timecard reach two pay runs and pay them twice."
            )
        if owners:
            return owners.pop()
        on -= timedelta(days=1)
    return None


def is_exempt_on(session: Session, employee_id: int, on: date) -> bool:
    """Is this employee FLSA-exempt on `on`?

    Exempt status gates whether hours are costed at all, which makes it part of
    the PRICED population every suppression gate counts — the D1 Critical was
    exactly a gate counting assigned rather than priced employees. So this must
    give one answer per employee, not one per property.

    **Mixed positions resolve to NON-exempt.** Someone whose assignments carry an
    exempt position at one property and a non-exempt one at the other is treated
    as non-exempt. That is both the protective answer and the legally coherent
    one: FLSA exemption turns on duties and salary basis for the workweek, and
    performing non-exempt work generally defeats it. Taking the exempt answer
    would let a property omit real hourly cost from Schedule 14.
    """
    positions = [
        a.position_id for a in assignments_on(session, employee_id, on)
        if a.position_id is not None
    ]
    # Silence is NOT exemption. An assignment with no position, or no effective
    # assignment at all, means we have not been told this person is exempt --
    # and exempt zeroes their labor cost and drops them out of the priced
    # population every suppression gate counts. Default to the protective answer,
    # consistent with "mixed resolves to non-exempt".
    if not positions:
        return False

    for position_id in positions:
        position = session.get(Position, position_id)
        if position is None or not position.flsa_exempt:
            return False
    return True


class MixedExemptionError(Exception):
    """An employee's FLSA-exempt status CHANGED partway through one pay period.

    Raised rather than answered, because no answer here is anything but a legal
    guess. Exemption turns on duties and salary basis assessed over the
    WORKWEEK, so a promotion on Wednesday raises a real question about how that
    week's overtime accumulates — and encoding a guess would either underpay
    real overtime or invent premium nobody owes.

    Same stance as `rules_for()` refusing an unencoded jurisdiction and
    `promote_timecard` refusing a card spanning two states: where the law is
    genuinely unsettled, stop and make a person decide.
    """


def resolve_exemption(
    session: Session, employee_id: int, dates: Iterable[date]
) -> bool:
    """ONE exempt answer for a whole card, or a refusal. Use this — never a
    single `is_exempt_on` sample — anywhere a period is priced.

    Sampling once at `period_start` and applying it to all 14 days is a bug that
    reached the payroll provider: a manager who moved to hourly mid-period had
    every subsequent day priced under day 1's exempt status, reproduced as $160
    of unpaid overtime SUBMITTED.

    It survived its first fix twice over. The fix went into `labor.py` (the
    estimate) and not `payroll_run.py` (the path that actually pays). And the
    estimate's own per-day retry read a `rate` that day-1 exemption had already
    set to `None`, so it could only ever turn pay off, never on — a mutation
    collapsing it back to a single sample passed all 913 tests.

    Both are call-site bugs. That is why the question now lives in a function
    instead of in a comment describing how to answer it.
    """
    statuses = {is_exempt_on(session, employee_id, d) for d in dates}
    if len(statuses) > 1:
        raise MixedExemptionError(
            f"employee {employee_id} is FLSA-exempt on some days of this period "
            "and non-exempt on others. Refusing to price the period: how "
            "overtime accumulates across a mid-workweek status change is a legal "
            "question this system does not answer. Split the pay period, or "
            "correct the assignment dates."
        )
    return statuses.pop() if statuses else False


def employee_serves_property(
    session: Session, employee_id: int, property_id: str, on: date
) -> bool:
    """Does this employee WORK at this property on `on`?

    This is the AUTHORIZATION question, and it is deliberately WIDER than the
    payment question: Vikram may punch at the 58033 kiosk and appear on its
    roster even though SJCES issues his paycheck. Using the primary assignment
    here would lock people out of the second hotel they actually work at.
    """
    return property_id in property_ids_on(session, employee_id, on)


def employee_ids_serving_property(
    session: Session, property_id: str, on: date
) -> set[int]:
    """Everyone who works at this property on `on` — primary or not.

    Filters through `in_effect_on` — THE date predicate — rather than a
    re-derived status+dating combination. The old inline copy said
    `status == "active"` unconditionally, which disagreed with the punch
    authorization for an end-dated-but-inactive assignment: punch-eligible
    yet invisible to kiosk search/identify/roster, i.e. unable to
    self-identify in face-first mode (F8; the exact re-derivation
    `in_effect_on`'s own docstring warns about)."""
    rows = session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.property_id == property_id,
        )
    ).scalars()
    return {a.employee_id for a in rows if in_effect_on(a, on)}


def property_ids_on(session: Session, employee_id: int, on: date) -> set[str]:
    """Every property this employee is assigned to on `on`.

    This is the ALLOCATION population — the properties whose P&Ls may carry a
    share of their cost — which is deliberately wider than the single primary
    that issues their paycheck.
    """
    return {a.property_id for a in assignments_on(session, employee_id, on)}
