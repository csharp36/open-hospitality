"""Kiosk devices and the passwordless punch API (Pillar B1).

TWO auth populations live here, so there are TWO routers:

- `admin_router` — operator-gated (org_admin / property_gm, GM confined to its own
  property): enroll, list, revoke devices. Included WITH `require_operator`.
- `kiosk_router` — authenticated by the DEVICE TOKEN, not an operator session.
  Included WITHOUT `require_operator`. (Added in Task 6.)

The device token is a bearer credential: high-entropy random, stored as a SHA-256
hash, surfaced in plaintext exactly once at enrollment, property-scoped, revocable.
A punch is therefore DEVICE-authenticated, not user-authenticated — a deliberate,
documented trust limitation inherited from the pilot's real flow. Buddy-punching is
deterred by the live photo + GM review at approval (B2), not by a secret.
"""

import hashlib
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.attendance import business_date_for
from usali.auth import (
    ORG_ADMIN,
    PROPERTY_GM,
    Principal,
    effective_roles,
    request_session_factory,
    require_grants,
)
from usali.biometric_rules import UnknownBiometricJurisdiction, biometric_rules_for
from usali.config import get_settings
from usali.assignments import (
    employee_ids_serving_property,
    employee_serves_property,
)
from usali.face_match import (
    FaceEmbedder,
    Template,
    cosine_similarity,
    embedding_from_bytes,
    identify,
)
from usali.models import (
    AuditEvent,
    Department,
    Employee,
    EmployeeFaceTemplate,
    KioskDevice,
    Property,
    Punch,
    Schedule,
    Shift,
)
from usali.photo_store import JPEG_MAGIC, PhotoStore, org_prefixed_key
from usali.tenancy import SessionFactory, current_org_id
from usali.timecards import (
    assemble_timecard,
    punch_state,
    punch_states,
    refuse_punch,
)
from usali.workforce import assignment_scope

require_kiosk_admin = require_grants(ORG_ADMIN, PROPERTY_GM)

admin_router = APIRouter(prefix="/api")
kiosk_router = APIRouter(prefix="/api/kiosk")


def _operator_session(request: Request) -> Session:
    """A session for the OPERATOR-authenticated admin routes: bound to the
    request's auth-resolved active org (L3, auth.require_active_org)."""
    factory = request_session_factory(request)
    return factory()


def _device_session(request: Request) -> Session:
    """A session for the DEVICE-authenticated kiosk routes: X-Kiosk-Token
    carries no OIDC claim to resolve an org from, so the device surface
    stays founding-org-bound — kiosk multi-org is deferred alongside the
    provisioning work (L6+), where the device row itself can name its org."""
    factory: SessionFactory = request.app.state.device_session_factory
    return factory()


def mint_device_token() -> tuple[str, str]:
    """Return (plaintext_token, sha256_hash). The plaintext is shown ONCE."""
    token = secrets.token_urlsafe(32)
    return token, hash_device_token(token)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class EnrollBody(BaseModel):
    property_id: str = Field(alias="property")
    name: str


class EnrolledModel(BaseModel):
    device_id: int
    property_id: str
    name: str
    token: str  # plaintext — the ONLY time it is ever returned


class DeviceModel(BaseModel):
    device_id: int
    property_id: str
    name: str
    revoked: bool


@admin_router.post("/kiosk-devices", status_code=201)
def enroll_device(
    body: EnrollBody, request: Request,
    principal: Principal = Depends(require_kiosk_admin),
) -> EnrolledModel:
    """Enroll a kiosk. A property_gm may only enroll into its own property."""
    with _operator_session(request) as session:
        if ORG_ADMIN not in effective_roles(session, principal.subject):
            scope = assignment_scope(principal, session)
            if not scope.allows_property(body.property_id):
                raise HTTPException(status_code=403, detail="property out of scope")
        if session.get(Property, body.property_id) is None:
            raise HTTPException(status_code=404, detail="unknown property")
        token, token_hash = mint_device_token()
        device = KioskDevice(
            property_id=body.property_id, name=body.name,
            token_hash=token_hash, enrolled_by=principal.subject,
        )
        session.add(device)
        session.add(
            AuditEvent(
                actor_subject=principal.subject, action="enroll_kiosk_device",
                resource_type="kiosk_device", resource_id=body.property_id,
            )
        )
        session.flush()
        result = EnrolledModel(
            device_id=device.device_id, property_id=device.property_id,
            name=device.name, token=token,
        )
        session.commit()
        return result


@admin_router.get("/kiosk-devices")
def list_devices(
    request: Request, principal: Principal = Depends(require_kiosk_admin)
) -> list[DeviceModel]:
    """Kiosks visible to the caller's scope."""
    with _operator_session(request) as session:
        scope = assignment_scope(principal, session)
        is_org_admin = ORG_ADMIN in effective_roles(session, principal.subject)
        devices = session.execute(select(KioskDevice)).scalars().all()
        visible = [
            d for d in devices
            if is_org_admin or scope.allows_property(d.property_id)
        ]
        return [
            DeviceModel(
                device_id=d.device_id, property_id=d.property_id, name=d.name,
                revoked=d.revoked_at is not None,
            )
            for d in visible
        ]


@admin_router.post("/kiosk-devices/{device_id}/revoke")
def revoke_device(
    device_id: int, request: Request,
    principal: Principal = Depends(require_kiosk_admin),
) -> DeviceModel:
    """Revoke a kiosk immediately — its token stops authenticating punches."""
    with _operator_session(request) as session:
        device = session.get(KioskDevice, device_id)
        if device is None:
            raise HTTPException(status_code=404, detail="device not found")
        if ORG_ADMIN not in effective_roles(session, principal.subject):
            scope = assignment_scope(principal, session)
            if not scope.allows_property(device.property_id):
                raise HTTPException(status_code=403, detail="property out of scope")
        device.revoked_at = datetime.now(UTC)
        session.add(
            AuditEvent(
                actor_subject=principal.subject, action="revoke_kiosk_device",
                resource_type="kiosk_device", resource_id=str(device_id),
            )
        )
        session.commit()
        return DeviceModel(
            device_id=device_id, property_id=device.property_id, name=device.name,
            revoked=True,
        )


_PUNCH_ORDER = ("clock_in", "lunch_start", "lunch_end", "clock_out")
_PUNCH_TYPES = frozenset(_PUNCH_ORDER)

# A punch photo is a webcam still; anything larger is abuse, not evidence.
_MAX_PHOTO_BYTES = 2 * 1024 * 1024

# A second identical punch inside this window is a double-tap, not a real event.
# Without this, one bounce mints two clock_ins and silently corrupts hours.
_PUNCH_DEBOUNCE_SECONDS = 60


def require_kiosk_device(
    request: Request,
    x_kiosk_token: Annotated[str | None, Header(alias="X-Kiosk-Token")] = None,
) -> KioskDevice:
    """Authenticate the DEVICE (not a user). 401 when the token is missing or
    unknown; 403 when the device has been revoked."""
    if not x_kiosk_token:
        raise HTTPException(status_code=401, detail="missing kiosk token")
    with _device_session(request) as session:
        device = session.execute(
            select(KioskDevice).where(
                KioskDevice.token_hash == hash_device_token(x_kiosk_token)
            )
        ).scalar_one_or_none()
        if device is None:
            raise HTTPException(status_code=401, detail="unknown kiosk token")
        if device.revoked_at is not None:
            raise HTTPException(status_code=403, detail="kiosk revoked")
        now = datetime.now(UTC)
        # Telemetry only — write at most once every 5 minutes, not on every poll.
        if device.last_seen_at is None or (now - device.last_seen_at) > timedelta(minutes=5):
            device.last_seen_at = now
            session.commit()
        session.expunge(device)
        return device


def _photo_store(request: Request) -> PhotoStore:
    store: PhotoStore = request.app.state.photo_store
    return store


def _matching_available(session: Session, property_id: str) -> bool:
    """Matching runs only where the flag is on AND the property's state has
    an ENCODED biometric posture (F4). An unencoded state silently gets the
    pre-F kiosk — the punch flow, never biometric processing."""
    if not get_settings().biometric_matching_enabled:
        return False
    jurisdiction = session.execute(
        select(Property.wage_jurisdiction).where(
            Property.property_id == property_id
        )
    ).scalar_one_or_none()
    if jurisdiction is None:
        return False
    try:
        biometric_rules_for(jurisdiction)
    except UnknownBiometricJurisdiction:
        return False
    return True


def _face_engine(request: Request) -> FaceEmbedder:
    engine: FaceEmbedder = request.app.state.get_face_engine()
    return engine


def _verify_punch_photo(
    request: Request, session: Session, employee_id: int,
    property_id: str, photo_bytes: bytes,
) -> tuple[str | None, float | None]:
    """Server-side 1:1 verification of the punch photo against the CLAIMED
    employee's template. The client never sets match fields — a kiosk that
    claims 'verified' is ignored; this derivation is the record.

    Degrades, NEVER blocks (the wage rule): matching off / unencoded state
    → (None, None), 'recorded without matching'; no template → no_template;
    stale-model template or faceless frame → unverified with no score; an
    engine failure of any kind → (None, None). The score is clamped to
    [0, 1] — cosine can go negative and the CHECK would otherwise turn the
    most alien face into a 500, i.e. a blocked punch.

    The try covers the TEMPLATE too, not just the engine: EncryptedBytes
    decrypts when the row loads, so a rotated field_encryption_key raises
    right there — and a truncated, dimension-drifted, or all-zero stored
    blob raises in decode/cosine. Every one of those is 'matching could
    not run', never a blocked punch (the F8 money-lens Critical)."""
    if not _matching_available(session, property_id):
        return None, None
    try:
        template = session.execute(
            select(EmployeeFaceTemplate).where(
                EmployeeFaceTemplate.employee_id == employee_id
            )
        ).scalar_one_or_none()
        if template is None:
            return "no_template", None
        engine = _face_engine(request)
        if template.model_version != engine.model_version:
            # Not comparable — never scored. Grey, so a human looks.
            return "unverified", None
        probe = engine.embed_largest_face(photo_bytes)
        if probe is None:
            return "unverified", None
        score = cosine_similarity(
            probe, embedding_from_bytes(template.embedding)
        )
        score = max(0.0, min(1.0, score))
        threshold = get_settings().face_match_threshold
        return ("verified" if score >= threshold else "unverified"), score
    except Exception:
        # Matching infrastructure down (models missing, runtime error) or
        # the stored template itself is damaged. The punch records without
        # matching — which is exactly what happened.
        return None, None


class KioskEmployeeModel(BaseModel):
    employee_id: int
    full_name: str


class KioskRosterEmployeeModel(KioskEmployeeModel):
    """A roster tile. Carries the punch state; the SEARCH model deliberately
    does not.

    The difference is what each surface already discloses. The roster lists
    every active name on the screen, so "who is on the clock" is one more
    operational fact about people already shown. Search answers a NAME you
    typed — adding state there would let anyone at the device probe whether a
    specific person is working right now, which is a fact about someone who is
    otherwise not on this screen at all.
    """

    # "out" | "in" | "on_break".
    state: str


class PunchedModel(BaseModel):
    punch_id: int
    employee_id: int
    punch_type: str
    business_date: str


class PunchStateModel(BaseModel):
    employee_id: int
    # "out" | "in" | "on_break" — what the employee's LAST punch left them in.
    state: str
    # The punches that are legal from here. The kiosk offers exactly these, and
    # the punch endpoint enforces the same rule: the screen is a courtesy, the
    # server is the gate.
    allowed: list[str]


@kiosk_router.get("/punch-state")
def kiosk_punch_state(
    request: Request,
    employee_id: int,
    device: KioskDevice = Depends(require_kiosk_device),
) -> PunchStateModel:
    """What this employee may punch next.

    Scoped exactly like /punch — one 403 for every denial, so this cannot be
    used to enumerate which employee ids exist.
    """
    with _device_session(request) as session:
        employee = session.get(Employee, employee_id)
        if (
            employee is None
            or not employee_serves_property(
                session, employee_id, device.property_id, date.today()
            )
            or employee.employment_status != "active"
        ):
            raise HTTPException(status_code=403, detail="employee out of kiosk scope")
        state = punch_state(session, employee_id)
        return PunchStateModel(
            employee_id=employee_id,
            state=state,
            allowed=[t for t in _PUNCH_ORDER if refuse_punch(state, t) is None],
        )


@kiosk_router.get("/employees")
def kiosk_roster(
    request: Request, device: KioskDevice = Depends(require_kiosk_device)
) -> list[KioskRosterEmployeeModel]:
    """The tap-to-punch roster: ACTIVE employees at this kiosk's property only.
    Deliberately returns no PII beyond the name shown on the tile."""
    with _device_session(request) as session:
        # Everyone ASSIGNED to this property — not just those whose paycheck it
        # issues. Someone working both hotels must appear on both rosters.
        serving = employee_ids_serving_property(
            session, device.property_id, date.today()
        )
        employees = (
            session.execute(
                select(Employee).where(
                    Employee.employee_id.in_(serving),
                    # `active` only (E3): leave and inactive staff vanish from
                    # the roster rather than tap in against their own record.
                    Employee.employment_status == "active",
                )
            ).scalars().all()
            if serving
            else []
        )
        states = punch_states(session, [e.employee_id for e in employees])
        return [
            KioskRosterEmployeeModel(
                employee_id=e.employee_id,
                full_name=e.full_name,
                state=states.get(e.employee_id, "out"),
            )
            for e in employees
        ]


class MyWeekShiftModel(BaseModel):
    business_date: str
    department: str  # name only — the kiosk shows words, not ids
    start_time: str  # "HH:MM" (D1's serialization)
    end_time: str  # "HH:MM"
    crosses_midnight: bool


class MyWeekModel(BaseModel):
    employee_id: int
    week_start: str
    published: bool  # False = no PUBLISHED schedule for this week (drafts invisible)
    shifts: list[MyWeekShiftModel]


@kiosk_router.get("/my-week")
def my_week(
    request: Request,
    employee_id: int,
    week_start: date,
    device: KioskDevice = Depends(require_kiosk_device),
) -> MyWeekModel:
    """The tapped employee's OWN shifts for one week, from the PUBLISHED
    schedule only — a draft is invisible to the kiosk by design. Both params
    are REQUIRED: the kiosk UI computes the grid Monday client-side, which
    keeps this endpoint dumb. HOURS-and-times only — never money.
    """
    with _device_session(request) as session:
        employee = session.get(Employee, employee_id)
        # A kiosk may only read its OWN property's ACTIVE staff. A single 403
        # covers every denial — unknown id, another property's id, not active
        # (terminated, leave, inactive) — so the endpoint is neither an
        # existence oracle nor an employment-STATUS oracle: the device never
        # learns WHICH rule refused it (the punch endpoint's collapse).
        if (
            employee is None
            or not employee_serves_property(
                session, employee_id, device.property_id, date.today()
            )
            or employee.employment_status != "active"
        ):
            raise HTTPException(status_code=403, detail="employee out of kiosk scope")

        # Unlike a punch (which leaves a Punch row + photo), my-week is a
        # silent read — so every SUCCESSFUL read leaves an audit row, denials
        # none (the egress-only house rule). The device IS the actor: there is
        # no principal on the kiosk router. Committed WITH the read below.
        session.add(
            AuditEvent(
                actor_subject=f"kiosk:{device.device_id}", action="kiosk_my_week",
                resource_type="employee", resource_id=str(employee_id),
            )
        )

        sched = session.execute(
            select(Schedule).where(
                Schedule.property_id == device.property_id,
                Schedule.week_start == week_start,
                Schedule.status == "published",
            )
        ).scalar_one_or_none()
        if sched is None:
            # `published: false` is still an egress — audited above.
            result = MyWeekModel(
                employee_id=employee_id, week_start=week_start.isoformat(),
                published=False, shifts=[],
            )
            session.commit()
            return result

        rows = session.execute(
            select(Shift, Department.name)
            .join(Department, Shift.department_id == Department.department_id)
            .where(
                Shift.schedule_id == sched.schedule_id,
                Shift.employee_id == employee_id,  # OWN shifts only
            )
            .order_by(Shift.business_date, Shift.start_time)
        ).all()
        result = MyWeekModel(
            employee_id=employee_id, week_start=week_start.isoformat(),
            published=True,
            shifts=[
                MyWeekShiftModel(
                    business_date=shift.business_date.isoformat(),
                    department=department_name,
                    start_time=shift.start_time.strftime("%H:%M"),
                    end_time=shift.end_time.strftime("%H:%M"),
                    crosses_midnight=shift.crosses_midnight,
                )
                for shift, department_name in rows
            ],
        )
        session.commit()
        return result


@kiosk_router.post("/punch", status_code=201)
def punch(
    request: Request,
    employee_id: Annotated[int, Form()],
    punch_type: Annotated[str, Form()],
    photo: Annotated[UploadFile, File()],
    device: KioskDevice = Depends(require_kiosk_device),
) -> PunchedModel:
    """Record one immutable punch with its evidence photo."""
    if punch_type not in _PUNCH_TYPES:
        raise HTTPException(status_code=422, detail=f"unknown punch_type {punch_type}")
    with _device_session(request) as session:
        employee = session.get(Employee, employee_id)
        # A kiosk may only punch its OWN property's ACTIVE staff. ONE 403
        # covers every denial — unknown id, another property's id, not active
        # — matching roster and my-week. Until the E3 review this endpoint
        # 404'd unknown ids first, which made it an existence oracle: a device
        # could enumerate which employee_ids exist (404 absent, 403 present).
        # The BACKLOG'd asymmetry, closed.
        if (
            employee is None
            or not employee_serves_property(
                session, employee_id, device.property_id, date.today()
            )
            or employee.employment_status != "active"
        ):
            raise HTTPException(status_code=403, detail="employee out of kiosk scope")

        recent = session.execute(
            select(Punch)
            .where(
                Punch.employee_id == employee_id,
                Punch.punch_type == punch_type,
                Punch.punched_at
                >= datetime.now(UTC) - timedelta(seconds=_PUNCH_DEBOUNCE_SECONDS),
            )
            .limit(1)
        ).scalar_one_or_none()
        if recent is not None:
            raise HTTPException(status_code=409, detail="duplicate punch")

        # THE ORDER RULE. Enforced here and not only on the kiosk screen: this
        # endpoint takes a device token, so anything holding one could post a
        # lunch_start for someone who never clocked in. The costly case is
        # clocking out mid-lunch — the lunch never closes, so nothing is
        # deducted and the employee is paid straight through the break.
        #
        # Manager corrections do NOT come through here. They go to
        # POST /timecards/{id}/adjustments, which exists to add the punch
        # someone forgot and must stay able to do so out of order.
        refusal = refuse_punch(punch_state(session, employee_id), punch_type)
        if refusal is not None:
            raise HTTPException(status_code=409, detail=refusal)

        prop = session.get(Property, device.property_id)
        assert prop is not None
        punched_at = datetime.now(UTC)
        business_date = business_date_for(
            punched_at, prop.timezone,
            cutoff_hour=get_settings().punch_business_day_cutoff_hour,
        )

        # The key carries the org prefix (L5): the DEVICE surface is
        # founding-org-bound today (current_org_id == FOUNDING_ORG_ID via
        # device_session_factory), so this is org 1's key — per-org-CORRECT
        # already, and the photo seals under org 1's key. The L6 seam is
        # device->org tenancy: when the kiosk device row can name its own
        # org, current_org_id here resolves that org and this key + its
        # crypto follow with no change to the mechanism.
        photo_key = org_prefixed_key(
            current_org_id(session),
            f"{device.property_id}/{business_date.isoformat()}/{uuid4().hex}.bin",
        )
        data = photo.file.read(_MAX_PHOTO_BYTES + 1)
        if len(data) > _MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="photo too large")
        if not data.startswith(JPEG_MAGIC):
            raise HTTPException(status_code=422, detail="photo must be a JPEG")
        _photo_store(request).put(photo_key, data)

        match_state, match_score = _verify_punch_photo(
            request, session, employee_id, device.property_id, data
        )
        row = Punch(
            employee_id=employee_id, kiosk_device_id=device.device_id,
            punch_type=punch_type, punched_at=punched_at,
            business_date=business_date, photo_key=photo_key,
            match_state=match_state, match_score=match_score,
        )
        session.add(row)
        session.flush()  # row now has an id; business_date was set at construction

        # Materialize this employee's card for the punch's period and link every
        # in-window punch (including this one) to it, in the SAME transaction as
        # the punch insert — a punch and its card link must be atomic. Idempotent,
        # so re-punching reuses the one card. Without this, the review queue is
        # empty and the photo purge (which joins on punch.timecard_id) never runs.
        assemble_timecard(
            session, employee_id, row.business_date,
            anchor=get_settings().payroll_period_anchor,
        )

        result = PunchedModel(
            punch_id=row.punch_id, employee_id=employee_id, punch_type=punch_type,
            business_date=business_date.isoformat(),
        )
        session.commit()
        return result


class KioskConfigModel(BaseModel):
    matching_enabled: bool


class IdentifyResultModel(BaseModel):
    # "matched" | "no_match" | "no_face". Deliberately NO score field: the
    # raw similarity is a hill-climbing oracle to a device-token holder
    # (morph a probe until the number rises), and the confirm-tap UI never
    # needed it. no_template is collapsed into no_match before it gets
    # here — whether anyone at this property is enrolled is a database
    # fact the device population has no business reading (F8).
    state: str
    employee_id: int | None = None
    full_name: str | None = None


_SEARCH_MIN_CHARS = 3
_SEARCH_CAP = 5


@kiosk_router.get("/config")
def kiosk_config(
    request: Request, device: KioskDevice = Depends(require_kiosk_device)
) -> KioskConfigModel:
    """Whether THIS device runs the camera-first flow (F5 reads this):
    true only when the flag is on AND the device's state has an encoded
    biometric posture. False falls back to the roster kiosk unchanged."""
    with _device_session(request) as session:
        return KioskConfigModel(
            matching_enabled=_matching_available(session, device.property_id)
        )


@kiosk_router.post("/identify")
def kiosk_identify(
    request: Request,
    photo: Annotated[UploadFile, File()],
    device: KioskDevice = Depends(require_kiosk_device),
) -> IdentifyResultModel:
    """Photo -> the ONE candidate for the confirm tap, 1:N against THIS
    property's active enrolled staff only (a face enrolled at the other
    hotel is invisible here — device confinement is population
    confinement).

    Deliberately STATELESS: the probe photo is discarded, nothing is
    audited, no punch exists yet — the score on the eventual punch is the
    record (plan F3 note). The confirm tap then punches, and the punch
    endpoint re-verifies server-side; a lying client gains nothing here.
    An ambiguous top-two is no_match (decision 8): the kiosk falls back to
    search rather than guessing between siblings."""
    with _device_session(request) as session:
        if not _matching_available(session, device.property_id):
            raise HTTPException(
                status_code=409,
                detail="face matching is not enabled for this kiosk",
            )
        data = photo.file.read(_MAX_PHOTO_BYTES + 1)
        if len(data) > _MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="photo too large")
        if not data.startswith(JPEG_MAGIC):
            raise HTTPException(status_code=422, detail="photo must be a JPEG")

        try:
            engine = _face_engine(request)
            probe = engine.embed_largest_face(data)
        except Exception as exc:
            # Unlike the punch (which records regardless), identify has
            # nothing to degrade TO — tell the kiosk to use the search flow.
            raise HTTPException(
                status_code=503, detail="face matching unavailable"
            ) from exc
        if probe is None:
            return IdentifyResultModel(state="no_face")

        try:
            serving = employee_ids_serving_property(
                session, device.property_id, date.today()
            )
            rows = (
                session.execute(
                    select(EmployeeFaceTemplate, Employee)
                    .join(Employee,
                          Employee.employee_id
                          == EmployeeFaceTemplate.employee_id)
                    .where(
                        EmployeeFaceTemplate.employee_id.in_(serving),
                        Employee.employment_status == "active",
                        # A stale-model template is not comparable: excluded
                        # here (its owner identifies via search, grey) rather
                        # than poisoning identification for the whole roster.
                        EmployeeFaceTemplate.model_version
                        == engine.model_version,
                    )
                ).all()
                if serving
                else []
            )
            settings = get_settings()
            result = identify(
                probe,
                [
                    Template(t.employee_id, embedding_from_bytes(t.embedding),
                             t.model_version)
                    for t, _ in rows
                ],
                threshold=settings.face_match_threshold,
                margin=settings.face_match_margin,
                model_version=engine.model_version,
            )
        except Exception as exc:
            # One damaged template row (undecryptable after a key rotation,
            # truncated blob) would otherwise 500 the camera flow for the
            # whole roster — same handled 503 as an engine outage; the
            # frontend falls back to search (F8 money lens, F-1's sibling).
            raise HTTPException(
                status_code=503, detail="face matching unavailable"
            ) from exc
        if result.state == "matched":
            names = {e.employee_id: e.full_name for _, e in rows}
            assert result.employee_id is not None
            return IdentifyResultModel(
                state="matched", employee_id=result.employee_id,
                full_name=names[result.employee_id],
            )
        if result.state == "no_template":
            # A database fact, not a photo fact — collapsed (see model).
            return IdentifyResultModel(state="no_match")
        return IdentifyResultModel(state=result.state)


@kiosk_router.get("/search")
def kiosk_search(
    request: Request,
    q: str,
    device: KioskDevice = Depends(require_kiosk_device),
) -> list[KioskEmployeeModel]:
    """The no-roster fallback (decision 2): a failed/ambiguous match never
    blocks the punch, so an employee can always type themselves in. Min 3
    characters and a hard cap keep this a self-identification tool, not a
    browsable roster; SQL wildcards in the query are literal characters."""
    needle = q.strip()
    if len(needle) < _SEARCH_MIN_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"search needs at least {_SEARCH_MIN_CHARS} characters",
        )
    escaped = (
        needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    with _device_session(request) as session:
        serving = employee_ids_serving_property(
            session, device.property_id, date.today()
        )
        if not serving:
            return []
        employees = session.execute(
            select(Employee)
            .where(
                Employee.employee_id.in_(serving),
                Employee.employment_status == "active",
                Employee.full_name.ilike(f"%{escaped}%", escape="\\"),
            )
            .order_by(Employee.full_name)
            .limit(_SEARCH_CAP)
        ).scalars().all()
        return [
            KioskEmployeeModel(employee_id=e.employee_id, full_name=e.full_name)
            for e in employees
        ]
