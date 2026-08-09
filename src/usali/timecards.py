"""Timecard hours + warnings engine (Pillar B2).

A PURE function over a merged timeline of immutable `punch` rows and additive
`timecard_adjustment` rows — so a manager correction never mutates a punch, and
the whole engine is unit-testable with no database.

California context: a duty-free 30-minute meal period is owed before the end of
the 5th hour of work. B2 FLAGS violations; it does NOT price the premium — that
is the payroll provider's job (Pillar C). See the Pillar B design.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from decimal import Decimal
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.apportion import apportion
from usali.models import KioskDevice, Punch, Timecard, TimecardAdjustment
from usali.photo_store import PhotoStore

_LOG = logging.getLogger(__name__)

_MEAL_REQUIRED_AFTER_MINUTES = 300  # 5 hours
_MEAL_MINIMUM_MINUTES = 30
_PERIOD_DAYS = 14


@dataclass(frozen=True)
class Event:
    punch_type: str
    at: datetime
    # Where this event happened. A punch derives it from its kiosk device; an
    # adjustment carries it explicitly. None means "not recorded" — only
    # possible for pre-E1 adjustment rows.
    property_id: str | None = None


@dataclass(frozen=True)
class DayResult:
    business_date: date
    worked_minutes: int
    warnings: list[str]
    # Worked minutes split by the property each span was clocked in at. Sums
    # EXACTLY to worked_minutes. A None key holds minutes whose property was
    # never recorded; usali.attribution decides where those land.
    property_minutes: dict[str | None, int] = field(default_factory=dict)


def period_for(business_date: date, anchor: date) -> tuple[date, date]:
    """The deterministic biweekly period containing `business_date`."""
    index = (business_date - anchor).days // _PERIOD_DAYS
    start = anchor + timedelta(days=index * _PERIOD_DAYS)
    return start, start + timedelta(days=_PERIOD_DAYS - 1)


# How long an open shift stays open. A forgotten clock_out must not lock the
# employee out of tomorrow's clock_in -- the engine already flags the gap as
# `missing_clock_out`, and refusing the next punch would turn one person's
# mistake into an unpayable day.
_OPEN_SHIFT_MAX_HOURS = 18

PunchState = Literal["out", "in", "on_break"]

# What each punch type leaves the employee in. `lunch_end` returns to "in",
# which is why the map is on the punch and not on a hand-rolled if-chain.
_STATE_AFTER: dict[str, PunchState] = {
    "clock_in": "in",
    "lunch_start": "on_break",
    "lunch_end": "in",
    "clock_out": "out",
}

# The only legal next punch from each state. This is the ORDER RULE, in one
# place: you cannot start a lunch you are not clocked in for, cannot clock out
# of a shift you never started, and cannot clock out while still on break --
# that last one is the expensive case, because the open lunch is never
# deducted and the employee is paid straight through it.
_ALLOWED_NEXT: dict[PunchState, frozenset[str]] = {
    "out": frozenset({"clock_in"}),
    "in": frozenset({"lunch_start", "clock_out"}),
    "on_break": frozenset({"lunch_end"}),
}

# Why a punch was refused, in the employee's words -- this is read off a kiosk
# screen by someone who is late for their shift, not by an engineer.
_REFUSALS: dict[tuple[PunchState, str], str] = {
    ("in", "clock_in"): "already clocked in",
    ("on_break", "clock_in"): "already clocked in — end lunch first",
    ("out", "lunch_start"): "clock in before starting lunch",
    ("on_break", "lunch_start"): "lunch already started",
    ("out", "lunch_end"): "no lunch to end",
    ("in", "lunch_end"): "no lunch to end",
    ("out", "clock_out"): "not clocked in",
    ("on_break", "clock_out"): "end lunch before clocking out",
}


def punch_state(
    session: Session, employee_id: int, *, now: datetime | None = None
) -> PunchState:
    """Where this employee stands right now: out, on the clock, or on break.

    Derived from the LAST punch, across properties: clocking in at one hotel and
    out at another is a real shift, and `compute_day` already attributes those
    minutes to where the span started.

    An open shift older than `_OPEN_SHIFT_MAX_HOURS` reads as "out". Someone who
    forgot to clock out on Tuesday is not still on shift on Wednesday, and
    treating them as such would refuse every punch they make from then on.
    """
    now = now or datetime.now(UTC)
    last = session.execute(
        select(Punch)
        .where(Punch.employee_id == employee_id)
        .order_by(Punch.punched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is None:
        return "out"
    state = _STATE_AFTER.get(last.punch_type, "out")
    if state == "out":
        return "out"
    at = last.punched_at
    if at.tzinfo is None:  # SQLite hands back naive datetimes
        at = at.replace(tzinfo=UTC)
    if now - at > timedelta(hours=_OPEN_SHIFT_MAX_HOURS):
        return "out"
    return state


def punch_states(
    session: Session, employee_ids: Sequence[int], *, now: datetime | None = None
) -> dict[int, PunchState]:
    """`punch_state` for a whole roster, in one query.

    Reads only the lookback window rather than one "last punch" query per
    employee: a kiosk roster is every active person at the property, and a
    query per tile is what turns a wall tablet into a slow one.
    """
    now = now or datetime.now(UTC)
    if not employee_ids:
        return {}
    rows = session.execute(
        select(Punch)
        .where(
            Punch.employee_id.in_(employee_ids),
            Punch.punched_at >= now - timedelta(hours=_OPEN_SHIFT_MAX_HOURS),
        )
        .order_by(Punch.punched_at.asc())
    ).scalars()
    # Ascending, so the last write per employee IS their latest punch.
    state: dict[int, PunchState] = {}
    for row in rows:
        state[row.employee_id] = _STATE_AFTER.get(row.punch_type, "out")
    return {employee_id: state.get(employee_id, "out") for employee_id in employee_ids}


def refuse_punch(state: PunchState, punch_type: str) -> str | None:
    """The reason `punch_type` is illegal from `state`, or None if it is legal."""
    if punch_type in _ALLOWED_NEXT[state]:
        return None
    return _REFUSALS.get((state, punch_type), "punch out of order")


def compute_day(business_date: date, events: Sequence[Event]) -> DayResult:
    """Worked minutes (lunch excluded) and warnings for one business date.

    An unclosed span is NEVER silently paid — it is flagged and contributes zero,
    so a missed clock-out surfaces as a warning for the manager rather than as
    free hours or a guessed end time.
    """
    ordered = sorted(events, key=lambda e: e.at)
    warnings: list[str] = []

    shift_minutes = 0
    # Gross span minutes per property, before the lunch deduction. Worked
    # minutes are apportioned across these at the end rather than trying to
    # decide which property "owns" a meal break.
    gross_by_property: dict[str | None, int] = {}
    open_in: datetime | None = None
    open_in_property: str | None = None
    lunch_minutes = 0
    open_lunch: datetime | None = None
    first_in: datetime | None = None
    lunch_started: datetime | None = None

    for event in ordered:
        if event.punch_type == "clock_in":
            if open_in is not None:
                # A second clock_in while one is still open means the first span
                # was never closed. Flag it — do NOT silently drop those minutes.
                warnings.append("missing_clock_out")
            if first_in is None:
                first_in = event.at
            open_in = event.at
            open_in_property = event.property_id
        elif event.punch_type == "clock_out":
            if open_in is None:
                warnings.append("missing_clock_in")
            else:
                span = int((event.at - open_in).total_seconds() // 60)
                shift_minutes += span
                # The span belongs to where it STARTED. Clocking out at the
                # other hotel does not move the shift there.
                gross_by_property[open_in_property] = (
                    gross_by_property.get(open_in_property, 0) + span
                )
                open_in = None
                open_in_property = None
        elif event.punch_type == "lunch_start":
            if open_lunch is not None:
                # Same for lunch: an overwritten open lunch_start is a missing
                # lunch_end, not a free re-start. Otherwise the meal-break
                # compliance check is computed off a fiction.
                warnings.append("missing_lunch_end")
            open_lunch = event.at
            if lunch_started is None:
                lunch_started = event.at
        elif event.punch_type == "lunch_end":
            if open_lunch is None:
                warnings.append("missing_lunch_start")
            else:
                lunch_minutes += int((event.at - open_lunch).total_seconds() // 60)
                open_lunch = None

    if open_in is not None:
        warnings.append("missing_clock_out")
    if open_lunch is not None:
        warnings.append("missing_lunch_end")

    worked = max(shift_minutes - lunch_minutes, 0)

    # Meal-break compliance (flagged, never priced).
    if shift_minutes > _MEAL_REQUIRED_AFTER_MINUTES:
        if lunch_started is None:
            warnings.append("no_meal_break")
        elif first_in is not None:
            into_shift = int((lunch_started - first_in).total_seconds() // 60)
            if into_shift > _MEAL_REQUIRED_AFTER_MINUTES:
                warnings.append("late_meal_break")
    if lunch_started is not None and 0 < lunch_minutes < _MEAL_MINIMUM_MINUTES:
        warnings.append("short_meal_break")

    return DayResult(
        business_date=business_date,
        worked_minutes=worked,
        warnings=warnings,
        property_minutes=_apportion(worked, gross_by_property),
    )


def _apportion(total: int, gross: dict[str | None, int]) -> dict[str | None, int]:
    """Split `total` worked minutes across properties by gross span minutes.

    Minutes are integers, so the shared Decimal helper is used at quantum 1 and
    the result converted back. One implementation, not a fourth copy.
    """
    if not gross or sum(gross.values()) <= 0 or total <= 0:
        return {}
    shares = apportion(Decimal(total), gross, quantum=Decimal(1))
    return {k: int(v) for k, v in shares.items() if v > 0}


def period_in_progress(
    period_end: date, *, now: datetime, timezone: str, cutoff_hour: int
) -> bool:
    """H1 (decision 1): is a period ending `period_end` still receiving
    punches? True while the property's CURRENT business date — night-audit
    cutoff respected, the same resolution punch recording uses — is on or
    before `period_end`. THE approvability predicate: approving a period
    still in progress is the door every punch-onto-approved-card landing
    walked through (the F8 deferral)."""
    from usali.attendance import business_date_for

    return period_end >= business_date_for(now, timezone, cutoff_hour)


def assemble_timecard(
    session: Session, employee_id: int, in_period: date, *, anchor: date
) -> Timecard:
    """Get-or-create the employee's timecard for the period containing
    `in_period`, and link every punch in that window to it. Idempotent.

    NEVER links to an APPROVED card (H1, decision 2): approval locked the
    reviewed set, and a punch joining it silently would bypass the F6
    acknowledgment gate and never promote. The punch stays recorded but
    unlinked — a MARKER the pay-run preflight names (H4) and reopen (H3)
    resolves by relinking under a fresh review."""
    period_start, period_end = period_for(in_period, anchor)
    card = session.execute(
        select(Timecard).where(
            Timecard.employee_id == employee_id,
            Timecard.period_start == period_start,
        )
    ).scalar_one_or_none()
    if card is not None and card.status == "approved":
        return card
    if card is None:
        card = Timecard(
            employee_id=employee_id, period_start=period_start,
            period_end=period_end, status="open",
        )
        session.add(card)
        session.flush()

    punches = session.execute(
        select(Punch).where(
            Punch.employee_id == employee_id,
            Punch.business_date >= period_start,
            Punch.business_date <= period_end,
        )
    ).scalars().all()
    for punch in punches:
        punch.timecard_id = card.timecard_id
    session.flush()
    return card


def timecard_events(session: Session, card: Timecard) -> dict[date, list[Event]]:
    """The merged timeline per business date: immutable punches PLUS additive
    adjustments. Adjustments never mutate a punch — they are extra events."""
    events: dict[date, list[Event]] = {}
    punches = session.execute(
        select(Punch).where(Punch.timecard_id == card.timecard_id)
    ).scalars().all()
    # Device -> property, resolved once per card rather than per punch.
    device_property: dict[int, str] = {
        d.device_id: d.property_id
        for d in session.execute(
            select(KioskDevice).where(
                KioskDevice.device_id.in_({p.kiosk_device_id for p in punches})
            )
        ).scalars()
    } if punches else {}
    for p in punches:
        events.setdefault(p.business_date, []).append(
            Event(
                punch_type=p.punch_type,
                at=p.punched_at,
                property_id=device_property.get(p.kiosk_device_id),
            )
        )
    adjustments = session.execute(
        select(TimecardAdjustment).where(
            TimecardAdjustment.timecard_id == card.timecard_id
        )
    ).scalars().all()
    for a in adjustments:
        events.setdefault(a.business_date, []).append(
            Event(
                punch_type=a.punch_type,
                at=a.adjusted_at,
                # NULL for pre-E1 rows; usali.attribution allocates those.
                property_id=a.property_id,
            )
        )
    return events


def compute_timecard(session: Session, card: Timecard) -> list[DayResult]:
    """Every day of the card, computed from the merged timeline."""
    by_date = timecard_events(session, card)
    return [compute_day(d, by_date[d]) for d in sorted(by_date)]


def purge_approved_photos(
    session: Session, store: PhotoStore, *, retention_days: int
) -> int:
    """Delete punch photos for timecards approved longer ago than the retention
    window, clear the pointers, and stamp the card. Photos are evidence for
    review — once the card is approved and the window has passed, they are
    deleted. Returns the number of photos purged.

    This is IRREVERSIBLE, so the guards are deliberately narrow: only `approved`
    cards, only past the cutoff, only those not already purged. `photos_purged_at`
    makes a second run a no-op.

    Both the image and the pointer go, in that order — so a later read is a clean
    404 (`punch.photo_key is None`) rather than a dangling key. A backend failure
    on one key is logged and skipped: the pointer is KEPT (the image may still
    exist) and the card is left unstamped so the next run retries it, rather than
    aborting the whole batch half-purged.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    cards = session.execute(
        select(Timecard).where(
            Timecard.status == "approved",
            Timecard.approved_at.is_not(None),
            Timecard.approved_at < cutoff,
            Timecard.photos_purged_at.is_(None),
        )
    ).scalars().all()

    purged = 0
    for card in cards:
        punches = session.execute(
            select(Punch).where(
                Punch.timecard_id == card.timecard_id,
                Punch.photo_key.is_not(None),
            )
        ).scalars().all()
        done, failed = _delete_photos(store, punches)
        purged += done
        if failed == 0:
            card.photos_purged_at = datetime.now(UTC)

    # H3 (decision 3): an UNLINKED punch belongs to no card, so the
    # approved-card clock above can never reach its photo — without this
    # clause, being unlinked made a face image immortal. Its own age
    # (punched_at) is the only clock it has: past the same retention
    # window it purges; inside it it STAYS, because it is evidence for
    # the reopen review. No stamp exists here — the cleared pointer is
    # what makes the next run skip it (and a failed delete keeps the
    # pointer, so it is retried, same posture as the card path).
    orphans = session.execute(
        select(Punch).where(
            Punch.timecard_id.is_(None),
            Punch.photo_key.is_not(None),
            Punch.punched_at < cutoff,
        )
    ).scalars().all()
    done, _failed = _delete_photos(store, orphans)
    purged += done

    session.flush()
    return purged


def _delete_photos(store: PhotoStore, punches: Sequence[Punch]) -> tuple[int, int]:
    """Delete each punch's photo then clear its pointer, in that order — a
    later read is a clean 404, never a dangling key. A backend failure on
    one key is logged and skipped with the pointer KEPT (the image may
    still exist), so the next run retries it. Returns (purged, failed)."""
    purged = failed = 0
    for punch in punches:
        key = punch.photo_key
        assert key is not None  # guarded by the callers' queries
        try:
            store.delete(key)
        except Exception:  # noqa: BLE001 - one bad key must not strand the batch
            failed += 1
            _LOG.exception("punch photo purge failed for punch_id=%s", punch.punch_id)
            continue
        punch.photo_key = None
        purged += 1
    return purged, failed
