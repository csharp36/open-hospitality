"""Storage + read helpers for the CRM demand feed (Pillar J4).

`store_pull` writes ONE batch (the as-of identity) plus its append-only
snapshot rows — never an update: booking pace is a comparison of
snapshots over time, and overwriting would destroy the only data that
makes it computable (plan decision 3).

Readers are per STAY DATE, not per batch: `latest_demand` takes the
newest COVERING batch's voice for each day ("current demand" for the J5
surfaces); `demand_pace` pairs it with the previous covering batch's
voice so "140 on the books today vs 120 last pull" is computable.
Newest is (pulled_at, batch_id) — pulled_at is the honest as-of stamp,
batch_id breaks a same-instant tie deterministically. That ordering
picks which ROW is current; no guard compares it against business
clocks (demand is decision-support data, not money).

COVERING matters (the J7 money High): a batch speaks for every date
inside its declared horizon — including by silence. A newer pull that
covered a stay-date and stated nothing means the demand is GONE (the
block cancelled, the event dropped off the books), so the date drops
out of "current demand" instead of serving last pull's figure forever.
Older batches keep speaking only for dates OUTSIDE the newer horizon.

Labels are joined to the bounded snapshot column here — the ONE place
demand crosses from port shape to storage shape.
"""

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.crm_feed import CrmDemandPull, CrmFeedError
from usali.models import CrmDemandSnapshot, CrmPullBatch

# The snapshot column bound (String(300)); truncating display text at
# the write beats a DataError mid-pull.
_LABELS_MAX = 300


@dataclass(frozen=True)
class DemandDayRead:
    """One stay-date's demand as one batch saw it, denormalized with the
    batch identity so surfaces can say WHEN the figure is from."""

    stay_date: date
    rooms_on_books: int | None
    group_rooms: int | None
    event_covers: int | None
    labels: str
    batch_id: int
    pulled_at: datetime
    provider: str


@dataclass(frozen=True)
class PaceDay:
    """A stay-date's newest voice paired with its previous one.
    `previous` is None when only one batch has ever spoken for the day."""

    stay_date: date
    current: DemandDayRead
    previous: DemandDayRead | None


def _join_labels(labels: tuple[str, ...]) -> str:
    return ", ".join(labels)[:_LABELS_MAX]


def store_pull(
    session: Session,
    *,
    property_id: str,
    provider: str,
    horizon_start: date,
    horizon_end: date,
    pull: CrmDemandPull,
) -> CrmPullBatch:
    """Write one pull as one batch + snapshot rows. Flushes (ids become
    real); the CALLER commits — the endpoint owns the transaction so the
    batch and its AuditEvent land atomically.

    The pull is validated BEFORE anything is added to the session, so a
    refusal leaves no pending writes for the caller's audit commit to
    sweep in: a duplicate stay-date must not die as a raw IntegrityError,
    and a day outside the declared horizon must not be stored under a
    batch whose horizon says otherwise — a covering batch's silence is a
    cancellation (module docstring), so a rogue row would corrupt that
    reading (J7)."""
    seen: set[date] = set()
    for day in pull.days:
        if day.stay_date in seen:
            raise CrmFeedError(
                "provider returned duplicate demand for one stay date"
            )
        seen.add(day.stay_date)
        if not (horizon_start <= day.stay_date <= horizon_end):
            raise CrmFeedError(
                "provider returned a stay date outside the pulled horizon"
            )
    batch = CrmPullBatch(
        property_id=property_id, provider=provider,
        horizon_start=horizon_start, horizon_end=horizon_end,
    )
    session.add(batch)
    session.flush()
    for day in pull.days:
        session.add(CrmDemandSnapshot(
            batch_id=batch.batch_id,
            stay_date=day.stay_date,
            rooms_on_books=day.rooms_on_books,
            group_rooms=day.group_rooms,
            event_covers=day.event_covers,
            labels=_join_labels(day.labels),
        ))
    session.flush()
    return batch


def _covering_batches(
    session: Session, property_id: str, start: date, end: date
) -> list[CrmPullBatch]:
    """Batches whose declared horizon intersects the window, NEWEST
    first — newest is (pulled_at, batch_id). Property confinement comes
    from the BATCH — the only place a property is recorded (one copy
    that cannot disagree, J2)."""
    return list(session.execute(
        select(CrmPullBatch)
        .where(
            CrmPullBatch.property_id == property_id,
            CrmPullBatch.horizon_start <= end,
            CrmPullBatch.horizon_end >= start,
        )
        .order_by(
            CrmPullBatch.pulled_at.desc(),
            CrmPullBatch.batch_id.desc(),
        )
    ).scalars())


def _voices_by_date(
    session: Session, property_id: str, start: date, end: date
) -> dict[date, list[DemandDayRead | None]]:
    """Per stay-date, one entry per batch COVERING the date, newest
    first: the batch's snapshot voice, or None where it was silent (it
    covered the date and stated no demand — a cancellation, not a gap
    in knowledge)."""
    batches = _covering_batches(session, property_id, start, end)
    if not batches:
        return {}
    snaps = {
        (row.batch_id, row.stay_date): row
        for row in session.execute(
            select(CrmDemandSnapshot).where(
                CrmDemandSnapshot.batch_id.in_(
                    [b.batch_id for b in batches]
                ),
                CrmDemandSnapshot.stay_date >= start,
                CrmDemandSnapshot.stay_date <= end,
            )
        ).scalars()
    }
    voices: dict[date, list[DemandDayRead | None]] = {}
    for stay_date in sorted({d for (_, d) in snaps}):
        per_batch: list[DemandDayRead | None] = []
        for batch in batches:
            if not (batch.horizon_start <= stay_date
                    <= batch.horizon_end):
                continue
            snapshot = snaps.get((batch.batch_id, stay_date))
            per_batch.append(
                DemandDayRead(
                    stay_date=snapshot.stay_date,
                    rooms_on_books=snapshot.rooms_on_books,
                    group_rooms=snapshot.group_rooms,
                    event_covers=snapshot.event_covers,
                    labels=snapshot.labels,
                    batch_id=batch.batch_id,
                    pulled_at=batch.pulled_at,
                    provider=batch.provider,
                )
                if snapshot is not None
                else None
            )
        voices[stay_date] = per_batch
    return voices


def latest_demand(
    session: Session, property_id: str, start: date, end: date
) -> list[DemandDayRead]:
    """Current demand: the newest COVERING batch's voice per stay-date.
    A newer batch that covers only part of the window wins where its
    horizon reaches; where it covered a date and was silent, the date
    has no current demand (the cancellation rule, module docstring)."""
    return [
        history[0]
        for _, history in sorted(
            _voices_by_date(session, property_id, start, end).items()
        )
        if history and history[0] is not None
    ]


def demand_pace(
    session: Session, property_id: str, start: date, end: date
) -> list[PaceDay]:
    """A day whose current voice is silence has no current to pace
    against and is omitted; `previous` is None when the second-newest
    covering batch was silent or there is none."""
    return [
        PaceDay(
            stay_date=stay_date,
            current=history[0],
            previous=history[1] if len(history) > 1 else None,
        )
        for stay_date, history in sorted(
            _voices_by_date(session, property_id, start, end).items()
        )
        if history and history[0] is not None
    ]
