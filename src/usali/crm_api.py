"""The audited CRM pull endpoint (Pillar J4, plan decision 6).

A pull is an EXPLICIT, audited act — no background scheduler in this
pillar; cadence is a deployment concern (cron hits this endpoint) and
the demo pulls once at seed. Scheduler roles only (org_admin /
property_gm), property-confined via assignment scope, exactly the
schedule_api convention: demand is a manager surface.

The response carries counts + the dropped-field report (names only —
values were never read, J3). It NEVER carries demand labels: labels
surface on the scheduler read surfaces (J5), not in a write receipt
that may land in an HTTP log.

Refusals that pass confinement are audited (`crm_refresh_refused`, the
I6 lesson): a probe of the pull surface is itself an event. Feature-off
is a loud 503 naming the connect surface — the pinned shape (the plan
offered 404-the-router as the alternative): absence degrades to a named
blocker, never to a path that looks like a typo. Since OH-17 the blocker
it names is `/integrations` and no longer `USALI_CRM_PROVIDER`: the env
var seeds org 1's credential row once and is read by no request path, so
naming it would point an operator at a lever that does nothing.
"""

from datetime import date, datetime, timedelta
from typing import Annotated, NoReturn
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from usali.auth import (
    ORG_ADMIN,
    PROPERTY_GM,
    Principal,
    effective_roles,
    request_session_factory,
    require_grants,
)
from usali.crm_feed import CrmFeedError
from usali.crm_pull import latest_demand, store_pull
from usali.integrations import DEMAND_FEED, connected_provider
from usali.models import AuditEvent, Property
from usali.workforce import assignment_scope

require_crm_scheduler = require_grants(ORG_ADMIN, PROPERTY_GM)

router = APIRouter(prefix="/api/crm")

# Bounded pull horizon: property-local today .. +90 days (decision 6).
_HORIZON_DAYS = 90

# The read surface's widest legal window. Wider than any UI asks for; a
# bound exists so the endpoint cannot be walked into scanning years of
# snapshot history.
_READ_WINDOW_MAX_DAYS = 366


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


def _active_org_crm_provider(session: Session) -> str:
    """The demand provider for the request's ACTIVE org (OH-17). '' when the
    org has no credential row — feature OFF, exactly as the old
    `org_settings.crm_provider` sentinel degraded.

    A one-line delegation kept as a NAMED function rather than inlined: it is
    the name the L5 per-org pins and `checklist._probe_demand_feed` both cite
    as "what the pull endpoint means by connected", and the session it takes
    is org-bound, so both L2 walls confine the read to the active org — there
    is no env fallback, and none for org != 1 in particular (the mutant L5
    kills)."""
    return connected_provider(session, DEMAND_FEED)


def _require_property_access(
    session: Session, principal: Principal, property_id: str
) -> None:
    """403 unless the caller may act on this property. Deliberately
    `assignment_scope`, NOT `resolve_scope` (the schedule_api /
    timecard_api rule): a co-held global VIEW role must not widen pull
    authority over a property the caller is not assigned to."""
    if ORG_ADMIN in effective_roles(session, principal.subject):
        return
    if not assignment_scope(principal, session).allows_property(property_id):
        # Names neither the property nor whether it exists — a refusal
        # must not become an existence oracle.
        raise HTTPException(status_code=403, detail="property out of scope")


class RefreshBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    property_id: str = Field(alias="property")


class RefreshResult(BaseModel):
    batch_id: int
    property_id: str
    provider: str
    horizon_start: str
    horizon_end: str
    days_written: int
    dropped_fields: dict[str, int]


@router.post("/refresh", status_code=201)
def refresh_demand(
    body: RefreshBody, request: Request,
    principal: Principal = Depends(require_crm_scheduler),
) -> RefreshResult:
    with _session(request) as session:
        _require_property_access(session, principal, body.property_id)
        prop = session.get(Property, body.property_id)
        if prop is None:
            raise HTTPException(status_code=404, detail="property not found")

        def refused(status_code: int, detail: str) -> NoReturn:
            # The I6 lesson: a probe that passed confinement is an event.
            # Roll back first: the audit commit must never sweep in a
            # pending partial write (store_pull validates before adding
            # anything, but the invariant is enforced HERE, not assumed).
            session.rollback()
            session.add(AuditEvent(
                actor_subject=principal.subject,
                action="crm_refresh_refused",
                resource_type="property", resource_id=body.property_id,
            ))
            session.commit()
            raise HTTPException(status_code=status_code, detail=detail)

        provider_name = _active_org_crm_provider(session)
        feed = request.app.state.get_crm_feed(request_session_factory(request))
        if provider_name == "" or feed is None:
            refused(
                503,
                "no demand feed is connected for this tenant — connect "
                "Delphi or Tripleseat on /integrations",
            )
        if prop.crm_ref is None:
            refused(
                409,
                f"property {body.property_id} has no crm_ref in the mapping "
                "registry (mapping/properties.yaml) — declare one and "
                "re-seed before pulling demand",
            )

        today = datetime.now(ZoneInfo(prop.timezone)).date()
        start, end = today, today + timedelta(days=_HORIZON_DAYS)
        try:
            pull = feed.fetch_demand(prop.crm_ref, start, end)
            # store_pull validates the pull (duplicate stay dates, days
            # outside the horizon) before writing — provider misbehavior
            # either way, refused on the same audited path (J7).
            batch = store_pull(
                session, property_id=body.property_id,
                provider=provider_name,
                horizon_start=start, horizon_end=end, pull=pull,
            )
        except CrmFeedError as exc:
            # Adapter messages are status + fixed text by construction
            # (J3) — no provider body can transit through this detail.
            refused(502, f"crm pull failed: {exc}")
        session.add(AuditEvent(
            actor_subject=principal.subject, action="crm_refresh",
            resource_type="crm_pull_batch", resource_id=str(batch.batch_id),
        ))
        session.commit()
        return RefreshResult(
            batch_id=batch.batch_id, property_id=body.property_id,
            provider=provider_name,
            horizon_start=start.isoformat(), horizon_end=end.isoformat(),
            days_written=len(pull.days),
            dropped_fields=dict(pull.dropped_fields),
        )


class DemandCapabilitiesModel(BaseModel):
    rooms_on_books: bool
    group_rooms: bool
    event_covers: bool


class DemandDayModel(BaseModel):
    stay_date: str
    rooms_on_books: int | None
    group_rooms: int | None
    event_covers: int | None
    labels: str
    pulled_at: str  # the as-of stamp — surfaces say WHEN a figure is from


class DemandSurfaceModel(BaseModel):
    configured: bool
    provider: str | None
    capabilities: DemandCapabilitiesModel
    days: list[DemandDayModel]


_NOT_CONFIGURED = DemandSurfaceModel(
    configured=False, provider=None,
    capabilities=DemandCapabilitiesModel(
        rooms_on_books=False, group_rooms=False, event_covers=False,
    ),
    days=[],
)


@router.get("/demand")
def get_demand(
    request: Request,
    property_id: Annotated[str, Query(alias="property")],
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    principal: Principal = Depends(require_crm_scheduler),
) -> DemandSurfaceModel:
    """The scheduler read surface (plan decision 4): the LATEST snapshot
    per stay-date, capability-labeled so an absent dimension renders as
    a gap, never a silent 0. Demand INFORMS the forecast — nothing here
    is a target or an auto-fill, and the kiosk has no route to any of
    it. Feature-off DEGRADES (configured: false) rather than refusing:
    a read surface with nothing to say is an honest gap, unlike the
    pull, where a silent no-op would eat an operator's explicit act."""
    if end < start:
        raise HTTPException(status_code=422,
                            detail="end must not precede start")
    if (end - start).days > _READ_WINDOW_MAX_DAYS:
        raise HTTPException(
            status_code=422,
            detail=f"window wider than {_READ_WINDOW_MAX_DAYS} days",
        )
    with _session(request) as session:
        _require_property_access(session, principal, property_id)
        if session.get(Property, property_id) is None:
            raise HTTPException(status_code=404, detail="property not found")

        provider_name = _active_org_crm_provider(session)
        feed = request.app.state.get_crm_feed(request_session_factory(request))
        if provider_name == "" or feed is None:
            # Stored history is deliberately NOT served while off: with
            # no configured provider there is no capability story to
            # label it honestly.
            return _NOT_CONFIGURED

        caps = feed.capabilities()
        return DemandSurfaceModel(
            configured=True, provider=provider_name,
            capabilities=DemandCapabilitiesModel(
                rooms_on_books=caps.emits_rooms_on_books,
                group_rooms=caps.emits_group_rooms,
                event_covers=caps.emits_event_covers,
            ),
            days=[
                DemandDayModel(
                    stay_date=row.stay_date.isoformat(),
                    rooms_on_books=row.rooms_on_books,
                    group_rooms=row.group_rooms,
                    event_covers=row.event_covers,
                    labels=row.labels,
                    pulled_at=row.pulled_at.isoformat(),
                )
                for row in latest_demand(session, property_id, start, end)
            ],
        )
