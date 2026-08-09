"""Operator-facing timecard review + approval (Pillar B2).

Approval is GM/org-admin only and property-confined via `assignment_scope` (so a
co-held global VIEW role — accountant, payroll_admin — cannot grant approval
authority over a property the caller is not actually assigned to: the A2.3 rule).
Approval LOCKS the card: no further adjustments, no re-approval.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import (
    ORG_ADMIN,
    PROPERTY_GM,
    Principal,
    effective_roles,
    request_session_factory,
    require_grants,
)
from usali.config import get_settings
from usali.labor import demote_timecard, promote_timecard
from usali.assignments import (
    assignments_on,
    primary_assignment_on,
    property_ids_on,
)
from usali.models import (
    AuditEvent,
    Employee,
    KioskDevice,
    Property,
    Punch,
    Timecard,
    TimecardAdjustment,
)
from usali.photo_store import JPEG_MAGIC, PhotoNotFound, PhotoStore
from usali.timecards import assemble_timecard, compute_timecard, period_in_progress
from usali.workforce import assignment_scope

require_approver = require_grants(ORG_ADMIN, PROPERTY_GM)

router = APIRouter(prefix="/api")


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


def _photo_store(request: Request) -> PhotoStore:
    store: PhotoStore = request.app.state.photo_store
    return store


def _card_property(session: Session, card: Timecard) -> str:
    """The property whose GM controls this timecard: the PRIMARY assignment's.

    This was the SEVENTH authorization site reading Employee.property_id -- the
    Task 7 cutover found six. It gates read, approve and adjust on every
    timecard, so before this fix the controlling GM was whichever property the
    retiring column happened to name, and a GM scoped to one hotel could book an
    adjustment onto the other's P&L. It would also have hard-broken when the
    column is dropped in Task 9.

    Primary (not "any assignment") is deliberate: a timecard is a single card
    approved by a single manager, and its pay run is keyed on the primary too --
    so approval authority and payment authority stay with the same property.
    """
    primary = primary_assignment_on(session, card.employee_id, card.period_end)
    if primary is not None:
        return primary.property_id

    # No effective primary at period end -- a terminated or transferred
    # employee's final card. Use their LAST primary, including inactive rows, so
    # approval authority stays with the property that actually employed them for
    # that period rather than evaporating.
    for assignment in assignments_on(
        session, card.employee_id, card.period_end, active_only=False
    ):
        if assignment.is_primary:
            return assignment.property_id

    raise HTTPException(
        status_code=409,
        detail="timecard has no assigned property; fix the employee's assignments",
    )


def _require_property_access(
    session: Session, principal: Principal, property_id: str
) -> None:
    """403 unless the caller may act on this property.

    Deliberately `assignment_scope`, NOT `resolve_scope`: the latter short-circuits
    to all-properties for org-wide VIEW grants, which would let an accountant-plus-GM
    approve hours at a property they do not run.
    """
    if ORG_ADMIN in effective_roles(session, principal.subject):
        return
    scope = assignment_scope(principal, session)
    if not scope.allows_property(property_id):
        # The detail names neither the property nor the employee — a refusal must
        # not become a side channel for the thing it just refused.
        raise HTTPException(status_code=403, detail="property out of scope")


def _require_card_access(session: Session, principal: Principal, card: Timecard) -> None:
    _require_property_access(session, principal, _card_property(session, card))


class DayPunchModel(BaseModel):
    """One punch as the approval view needs it. `has_photo` is False both for
    a purged photo and one never stored — the manager-facing meaning ("no
    evidence to show") is the same, and distinguishing them here would leak
    the retention job's schedule for no review value."""

    punch_id: int
    punch_type: str
    punched_at: str
    has_photo: bool
    # F6: what server-side verification concluded at punch time (F4).
    # verified=green, unverified=red (gates approval), no_template=grey
    # (cold start), None=recorded before/without matching.
    match_state: str | None = None
    match_score: float | None = None


class DayModel(BaseModel):
    business_date: str
    worked_minutes: int
    warnings: list[str]
    punches: list[DayPunchModel] = []


class TimecardModel(BaseModel):
    timecard_id: int
    employee_id: int
    employee_name: str
    period_start: str
    period_end: str
    status: str
    total_minutes: int
    days: list[DayModel]


class TimecardSummaryModel(BaseModel):
    timecard_id: int
    employee_id: int
    employee_name: str
    period_start: str
    status: str
    total_minutes: int


def _to_model(session: Session, card: Timecard) -> TimecardModel:
    employee = session.get(Employee, card.employee_id)
    assert employee is not None
    days = compute_timecard(session, card)
    punch_rows = session.execute(
        select(Punch).where(Punch.timecard_id == card.timecard_id)
        .order_by(Punch.punched_at)
    ).scalars().all()
    by_date: dict[str, list[DayPunchModel]] = {}
    for p in punch_rows:
        by_date.setdefault(p.business_date.isoformat(), []).append(DayPunchModel(
            punch_id=p.punch_id, punch_type=p.punch_type,
            punched_at=p.punched_at.isoformat(),
            has_photo=p.photo_key is not None,
            match_state=p.match_state, match_score=p.match_score,
        ))
    return TimecardModel(
        timecard_id=card.timecard_id, employee_id=card.employee_id,
        employee_name=employee.full_name,
        period_start=card.period_start.isoformat(),
        period_end=card.period_end.isoformat(),
        status=card.status,
        total_minutes=sum(d.worked_minutes for d in days),
        days=[
            DayModel(business_date=d.business_date.isoformat(),
                     worked_minutes=d.worked_minutes, warnings=d.warnings,
                     punches=by_date.get(d.business_date.isoformat(), []))
            for d in days
        ],
    )


@router.get("/timecards")
def list_timecards(
    request: Request,
    principal: Principal = Depends(require_approver),
    status: Annotated[str | None, Query()] = None,
) -> list[TimecardSummaryModel]:
    """Timecards for properties the caller may approve. Out-of-scope cards are
    invisible (not merely un-approvable) — the list never leaks another
    property's employee names."""
    with _session(request) as session:
        cards = session.execute(select(Timecard)).scalars().all()
        out: list[TimecardSummaryModel] = []
        for card in cards:
            if status is not None and card.status != status:
                continue
            try:
                _require_card_access(session, principal, card)
            except HTTPException:
                continue  # simply not visible
            employee = session.get(Employee, card.employee_id)
            assert employee is not None
            days = compute_timecard(session, card)
            out.append(TimecardSummaryModel(
                timecard_id=card.timecard_id, employee_id=card.employee_id,
                employee_name=employee.full_name,
                period_start=card.period_start.isoformat(), status=card.status,
                total_minutes=sum(d.worked_minutes for d in days),
            ))
        return out


@router.get("/timecards/{timecard_id}")
def get_timecard(
    timecard_id: int, request: Request,
    principal: Principal = Depends(require_approver),
) -> TimecardModel:
    with _session(request) as session:
        card = session.get(Timecard, timecard_id)
        if card is None:
            raise HTTPException(status_code=404, detail="timecard not found")
        _require_card_access(session, principal, card)
        return _to_model(session, card)


class ApproveBody(BaseModel):
    # F6: the unverified punch ids the approver is explicitly vouching for.
    # Absent/empty means "this card needs no acknowledgments" — and the
    # refusal below names the ids if that turns out to be wrong.
    acknowledged_punch_ids: list[int] = []


@router.post("/timecards/{timecard_id}/approve")
def approve_timecard(
    timecard_id: int, request: Request,
    body: ApproveBody | None = None,
    principal: Principal = Depends(require_approver),
) -> TimecardModel:
    """Approve and LOCK. Starts the photo retention clock.

    F6 (plan decision 3): a card holding `unverified` punches REFUSES until
    each is explicitly acknowledged in the request — the approver is
    vouching, by punch, that a human looked at the red badge. Each
    acknowledgment writes its own audit row. `no_template` and NULL never
    gate: cold start and pre-matching punches must not deadlock approval.
    Acknowledging a punch that gates nothing is refused — the trail must
    never record an acknowledgment that had no effect."""
    acknowledged = set(body.acknowledged_punch_ids if body else [])
    with _session(request) as session:
        card = session.get(Timecard, timecard_id)
        if card is None:
            raise HTTPException(status_code=404, detail="timecard not found")
        _require_card_access(session, principal, card)
        if card.status == "approved":
            raise HTTPException(status_code=409, detail="timecard already approved")

        # H1 (decision 1): a period still in progress is not approvable —
        # a punch landing after approval would have to choose between
        # joining a locked card unreviewed (the F8 bypass) and going
        # unlinked/unpaid. No legitimate workflow approves a period
        # someone is still working in; refuse at the door, against the
        # controlling property's OWN business date (cutoff respected).
        prop = session.get(Property, _card_property(session, card))
        assert prop is not None
        if period_in_progress(
            card.period_end, now=datetime.now(UTC), timezone=prop.timezone,
            cutoff_hour=get_settings().punch_business_day_cutoff_hour,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"period runs through {card.period_end.isoformat()} and "
                    "is still in progress — punches can still land on it. "
                    f"Approvable from "
                    f"{(card.period_end + timedelta(days=1)).isoformat()}."
                ),
            )

        unverified_ids = set(session.execute(
            select(Punch.punch_id).where(
                Punch.timecard_id == timecard_id,
                Punch.match_state == "unverified",
            )
        ).scalars())
        stray = acknowledged - unverified_ids
        if stray:
            raise HTTPException(
                status_code=422,
                detail="acknowledged_punch_ids contains punches that are not "
                       f"unverified punches of this card: {sorted(stray)}",
            )
        missing = unverified_ids - acknowledged
        if missing:
            raise HTTPException(
                status_code=409,
                detail="cannot approve: unverified punches require explicit "
                       f"acknowledgment: {sorted(missing)}. Review each red "
                       "badge and re-approve with acknowledged_punch_ids.",
            )
        for punch_id in sorted(acknowledged):
            session.add(AuditEvent(
                actor_subject=principal.subject,
                action="acknowledge_unverified_punch",
                resource_type="punch", resource_id=str(punch_id),
            ))

        card.status = "approved"
        card.approved_by = principal.subject
        card.approved_at = datetime.now(UTC)
        session.add(AuditEvent(
            actor_subject=principal.subject, action="approve_timecard",
            resource_type="timecard", resource_id=str(timecard_id),
        ))
        # The card is now locked and audited — promote its approved hours to
        # estimated labor facts (idempotent; a re-approval 409s above and never
        # reaches here, so this cannot double-count).
        promote_timecard(session, card, anchor=get_settings().payroll_period_anchor)
        model = _to_model(session, card)
        session.commit()
        return model


@router.post("/timecards/{timecard_id}/reopen")
def reopen_timecard(
    timecard_id: int, request: Request,
    principal: Principal = Depends(require_approver),
) -> TimecardModel:
    """Reopen an APPROVED card for a fresh review — an explicit, authorized,
    audited act, never a side effect of a punch (H3, decision 3: a kiosk
    device token must not be able to undo a GM's approval, which is why the
    resolution for an unlinked punch is a human here and not auto-reopen).

    Unwinds exactly what approval did: status back to open, approval
    identity cleared, the card's estimated labor facts deleted (promotion
    is delete-then-rewrite keyed on timecard_id — re-approval re-promotes),
    and the period's unlinked punches relinked so the new review sees them.
    Re-approval re-runs the F6 gate over ALL linked punches, relinked ones
    included: acknowledgments do not carry over.

    Deliberately allowed after the period's pay run has submitted — the
    run's PayRunLine snapshot is immutable paid history, so reopening
    desyncs nothing; extra minutes surface through the pay-run preflight
    (H4 while unlinked, the paid-content comparison after relink), never
    through mutation of what was paid. `photos_purged_at` is CLEARED
    (H9 review): the stamp is false the moment a photo-bearing punch
    relinks, and it barred the card purge path while the link barred the
    orphan path — an immortal face image. Already-purged photos are
    still gone (their pointers are NULL; the purge loop is a no-op for
    them); the retention clock restarts at re-approval."""
    with _session(request) as session:
        card = session.get(Timecard, timecard_id)
        if card is None:
            raise HTTPException(status_code=404, detail="timecard not found")
        _require_card_access(session, principal, card)
        if card.status != "approved":
            raise HTTPException(
                status_code=409,
                detail="only an approved timecard can be reopened",
            )
        card.status = "open"
        card.approved_by = None
        card.approved_at = None
        card.photos_purged_at = None
        demote_timecard(session, card)
        # Relink AFTER the flip: assemble skips approved cards (H1).
        assemble_timecard(
            session, card.employee_id, card.period_start,
            anchor=get_settings().payroll_period_anchor,
        )
        session.add(AuditEvent(
            actor_subject=principal.subject, action="reopen_timecard",
            resource_type="timecard", resource_id=str(timecard_id),
        ))
        model = _to_model(session, card)
        session.commit()
        return model


_PUNCH_TYPES = frozenset({"clock_in", "lunch_start", "lunch_end", "clock_out"})


class AdjustmentBody(BaseModel):
    punch_type: str
    # AWARE, not naive: punches are tz-aware, and the two are merged into one
    # sorted timeline. A naive value would either break the sort or force us to
    # guess a zone — and a guessed zone silently moves someone's paid hours.
    adjusted_at: AwareDatetime
    business_date: date
    # The whole audit value of an adjustment is WHY. Bounded to the column width
    # so an over-long reason is a 422 at the door, not a 500 at the INSERT.
    reason: str = Field(min_length=1, max_length=300)
    # WHERE the corrected work happened. A punch carries this via its kiosk
    # device; an adjustment has no device, so without it the minutes cannot be
    # attributed and fall back to an ESTIMATE (usali.attribution). Optional
    # because a single-property employee has only one answer, which the endpoint
    # fills in for them; required in effect for anyone working two properties.
    property_id: str | None = None


@router.post("/timecards/{timecard_id}/adjustments", status_code=201)
def add_adjustment(
    timecard_id: int, body: AdjustmentBody, request: Request,
    principal: Principal = Depends(require_approver),
) -> TimecardModel:
    """Correct a timecard by ADDING a synthetic event. Punches stay immutable —
    this is the only correction path, and it is audited. Refused once the card is
    approved (locked)."""
    if body.punch_type not in _PUNCH_TYPES:
        # The engine ignores event types it does not know, so an unvalidated type
        # would persist a row that pays nothing and explains nothing.
        raise HTTPException(
            status_code=422, detail=f"unknown punch_type {body.punch_type}"
        )
    with _session(request) as session:
        card = session.get(Timecard, timecard_id)
        if card is None:
            raise HTTPException(status_code=404, detail="timecard not found")
        _require_card_access(session, principal, card)
        if card.status == "approved":
            raise HTTPException(status_code=409, detail="timecard is approved and locked")
        if not card.period_start <= body.business_date <= card.period_end:
            # Adjustments are keyed to the card by FK but GROUPED by business_date.
            # An out-of-period date would mint a phantom day on this card and book
            # hours into a period they did not happen in.
            raise HTTPException(
                status_code=422, detail="business_date is outside the timecard period"
            )
        if not card.period_start <= body.adjusted_at.date() <= card.period_end:
            # An in-period business_date with a typo'd adjusted_at (e.g. wrong
            # year) would book arbitrary hours: the engine computes spans off
            # adjusted_at, so bound its DATE to the same inclusive period too.
            raise HTTPException(
                status_code=422, detail="adjusted_at is outside the timecard period"
            )
        # Resolve WHERE the corrected work happened. With one assignment there
        # is only one answer, so we fill it in rather than making every caller
        # supply it. With two, an unspecified property would silently become an
        # estimate, so refuse instead — the manager knows which hotel it was.
        placements = property_ids_on(session, card.employee_id, body.business_date)
        if body.property_id is not None:
            # TWO different questions, and only the first was ever asked.
            #
            # (a) Is the EMPLOYEE assigned there? Previously guarded by
            #     `known and ...`, which skipped validation entirely when the
            #     employee had no placement that date -- so any FK-valid property
            #     was accepted. The `known` escape is gone: no placement on that
            #     date is a 422, not a free choice of P&L.
            if body.property_id not in placements:
                raise HTTPException(
                    status_code=422,
                    detail="employee is not assigned to that property on this date",
                )
            # (b) Does the CALLER have authority there? Nobody asked. Card access
            #     is checked against the card's property (the employee's primary),
            #     which says nothing about the property being written to -- so a
            #     GM scoped to one hotel could post synthetic hours onto the
            #     other hotel's Schedule 14 and its labor cost. Same class as the
            #     `terminate` bypass, different verb.
            _require_property_access(session, principal, body.property_id)
            adjustment_property = body.property_id
        elif len(placements) == 1:
            adjustment_property = next(iter(placements))
        elif len(placements) > 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    "property_id is required: this employee works at more than one "
                    "property on that date, so the hours cannot be attributed"
                ),
            )
        else:
            # No placement on that date at all -- a terminated or not-yet-hired
            # employee. The hours are real (a manager is correcting a punch), but
            # nothing here says WHERE, and an unattributed adjustment lands in
            # `attribution`'s proportional spread rather than on a guessed P&L.
            adjustment_property = None

        session.add(TimecardAdjustment(
            timecard_id=timecard_id, punch_type=body.punch_type,
            adjusted_at=body.adjusted_at, business_date=body.business_date,
            reason=body.reason, actor_subject=principal.subject,
            property_id=adjustment_property,
        ))
        session.add(AuditEvent(
            actor_subject=principal.subject, action="adjust_timecard",
            resource_type="timecard", resource_id=str(timecard_id),
        ))
        session.flush()
        model = _to_model(session, card)
        session.commit()
        return model


# The ONLY photo-read path in the system. B1 shipped with zero photo egress by
# design; this is the surface that adds it, and the object is an employee's face.
_PHOTO_HEADERS = {
    # A face image must not sit in a browser cache, a proxy, or a disk cache.
    "Cache-Control": "no-store",
    # Belt-and-braces with the honest content type below: even if a non-JPEG blob
    # somehow reached the store, no browser may sniff it into something it will
    # execute on our origin.
    "X-Content-Type-Options": "nosniff",
}


@router.get("/punches/{punch_id}/photo")
def punch_photo(
    punch_id: int, request: Request,
    principal: Principal = Depends(require_approver),
) -> Response:
    """The punch's evidence photo, for the approving manager's eyes.

    The ONLY photo-read path in the system. Approver-only, confined to the
    punch's property, never cached, and AUDITED on every read — a face image is
    PII. No face matching is performed anywhere; this is a human looking at a
    picture.

    Three orderings here are deliberate:

    * The audit row is written and COMMITTED before the response is built, and
      the body is a fully-buffered `Response`, never a `StreamingResponse`. So no
      photo byte can reach the wire without a durable "who viewed what" row
      behind it. The inverse — an audit row for a response the client never
      finished receiving — is the safe direction to fail.
    * The audit records EGRESS, so it is written on the success path only. A 403
      or a 404 released nothing; logging denials here would let any authenticated
      operator flood the PHI-access trail by hammering a URL, and the trail's
      value is that every row in it is a face somebody actually saw.
    * The content type follows the BYTES, not the store's optimism. The kiosk
      enforces the JPEG magic on upload, but the store is not the authority on
      what it holds; anything unrecognised goes out inert (octet-stream +
      attachment), never as something a browser will render.
    """
    with _session(request) as session:
        punch = session.get(Punch, punch_id)
        # A NULL photo_key is a punch whose photo was purged (or never stored):
        # indistinguishable, to a caller, from no such punch — which is right.
        if punch is None or punch.photo_key is None:
            raise HTTPException(status_code=404, detail="photo not found")
        employee = session.get(Employee, punch.employee_id)
        assert employee is not None
        # The property is the DEVICE's -- the kiosk that took this photo -- not
        # the employee's. For someone working two hotels those differ, and the
        # manager who may view the image is the one who runs the hotel where it
        # was taken.
        #
        # An out-of-scope approver still gets 403 (not 404), matching the rest of
        # this module. It tells them a punch id exists somewhere in the platform
        # — an existence oracle over an integer id, no PII, and only to a caller
        # who already holds approval authority at some property. Accepted.
        device = session.get(KioskDevice, punch.kiosk_device_id)
        assert device is not None
        _require_property_access(session, principal, device.property_id)
        try:
            data = _photo_store(request).get(punch.photo_key)
        except PhotoNotFound as exc:  # purged by the retention job
            raise HTTPException(status_code=404, detail="photo purged") from exc

        session.add(AuditEvent(
            actor_subject=principal.subject, action="view_punch_photo",
            resource_type="punch", resource_id=str(punch_id),
        ))
        session.commit()

    if data.startswith(JPEG_MAGIC):
        return Response(content=data, media_type="image/jpeg", headers=_PHOTO_HEADERS)
    # Not a JPEG: serve it as an inert download. No filename — the photo key
    # encodes the property and the business date, and none of that needs to ride
    # out on a header.
    return Response(
        content=data, media_type="application/octet-stream",
        headers={**_PHOTO_HEADERS, "Content-Disposition": "attachment"},
    )
