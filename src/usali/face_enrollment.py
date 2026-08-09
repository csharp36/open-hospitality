"""Face-template enrollment API (F3) — the ONE writer of templates.

Onboarder-gated (org_admin | property_gm) with the whole-person scope rule
from PATCH/terminate: a template is usable at EVERY property's kiosk, so a
scoped GM needs scope over every placement, not just their own hotel.

Three gates run before any pixel is processed, in refusal-first order:

1. The dark flag. Enrollment while `biometric_matching_enabled` is off
   would compute a biometric the flag claims isn't being processed.
2. The jurisdiction gate (`biometric_rules_for`): every placement property
   must have an ENCODED consent posture. An Illinois hire cannot be
   enrolled under California's notice flow (plan decision 6).
3. Employment: terminated people are not enrolled — separation is the
   deletion edge, not an enrollment state.

Enrollment records which notice-at-collection version the employee was
shown (`notice_version`/`notified_on` from settings; the paired CHECK
refuses half a fact). Re-enrollment REPLACES the single row. Writes are
audited by resource id — enroll/replace/delete only, never match events:
the score on the punch is the record (plan F3 note), and image bytes never
enter the audit trail.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.auth import ORG_ADMIN, Principal, effective_roles, request_session_factory
from usali.assignments import property_ids_on
from usali.biometric_rules import UnknownBiometricJurisdiction, biometric_rules_for
from usali.config import get_settings
from usali.face_match import FaceEmbedder, FaceModelsMissing, embedding_to_bytes
from usali.models import AuditEvent, Employee, EmployeeFaceTemplate, Property
from usali.photo_store import JPEG_MAGIC, PhotoStore, face_reference_key
from usali.tenancy import current_org_id
from usali.workforce import assignment_scope, require_onboarder

router = APIRouter(prefix="/api")

_MAX_PHOTO_BYTES = 2 * 1024 * 1024  # the kiosk's cap; same camera, same bound


def _session(request: Request) -> Session:
    factory = request_session_factory(request)
    return factory()


def _photo_store(request: Request) -> PhotoStore:
    store: PhotoStore = request.app.state.photo_store
    return store


def _engine(request: Request) -> FaceEmbedder:
    engine: FaceEmbedder = request.app.state.get_face_engine()
    return engine


class FaceTemplateModel(BaseModel):
    employee_id: int
    model_version: str
    notice_version: str | None


class FaceTemplateStatusModel(BaseModel):
    employee_id: int
    enrolled: bool
    model_version: str | None = None
    notice_version: str | None = None
    created_at: str | None = None


def write_face_template(
    session: Session,
    engine: FaceEmbedder,
    photo_store: PhotoStore,
    *,
    employee_id: int,
    photo_bytes: bytes,
    actor_subject: str,
    notice_version: str | None,
) -> str | None:
    """Embed the photo and write template + reference photo + audit row.

    The one writer both the API route and the demo seed land on, so replace
    semantics, the notice pair, and the audit vocabulary cannot drift apart.
    Returns the audit action written, or None when no face was found (in
    which case NOTHING is written). Raises FaceModelsMissing when the model
    files are absent. Does not commit — the caller owns the transaction.
    """
    embedding = engine.embed_largest_face(photo_bytes)
    if embedding is None:
        return None
    existing = session.execute(
        select(EmployeeFaceTemplate).where(
            EmployeeFaceTemplate.employee_id == employee_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        session.delete(existing)
        session.flush()  # replace, under the one-row-per-employee unique
    session.add(EmployeeFaceTemplate(
        employee_id=employee_id,
        embedding=embedding_to_bytes(embedding),
        model_version=engine.model_version,
        notice_version=notice_version,
        notified_on=date.today() if notice_version is not None else None,
        created_by=actor_subject,
    ))
    # The reference photo lands under the ACTIVE org's key prefix (L5): the
    # employee belongs to this org by the walls, so put here and delete on
    # termination/withdrawal rebuild the identical org-prefixed key.
    photo_store.put(
        face_reference_key(current_org_id(session), employee_id), photo_bytes
    )
    action = (
        "face_template_replaced" if existing is not None
        else "face_template_enrolled"
    )
    session.add(AuditEvent(
        actor_subject=actor_subject, action=action,
        resource_type="employee", resource_id=str(employee_id),
    ))
    return action


def _load_scoped_employee(
    session: Session, employee_id: int, principal: Principal
) -> Employee:
    """404 unknown, then the whole-person scope rule (PATCH/terminate)."""
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    if ORG_ADMIN not in effective_roles(session, principal.subject):
        scope = assignment_scope(principal, session)
        placements = property_ids_on(session, employee_id, date.today())
        if not placements:
            raise HTTPException(
                status_code=409,
                detail="employee has no active assignment to scope the "
                       "enrollment against",
            )
        if not all(scope.allows_property(p) for p in placements):
            raise HTTPException(status_code=403, detail="property out of scope")
    return employee


def _refuse_unencoded_jurisdictions(session: Session, employee_id: int) -> None:
    """Every placement property must carry an encoded biometric posture."""
    placements = property_ids_on(session, employee_id, date.today())
    for property_id in sorted(placements):
        jurisdiction = session.execute(
            select(Property.wage_jurisdiction).where(
                Property.property_id == property_id
            )
        ).scalar_one()
        if jurisdiction is None:
            raise HTTPException(
                status_code=422,
                detail=f"property {property_id} has no wage_jurisdiction set; "
                       "biometric enrollment requires an encoded posture "
                       "(docs/reference/biometric-attendance.md)",
            )
        try:
            biometric_rules_for(jurisdiction)
        except UnknownBiometricJurisdiction as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/employees/{employee_id}/face-template", status_code=201)
def enroll_face_template(
    employee_id: int, photo: UploadFile, request: Request,
    principal: Principal = Depends(require_onboarder),
) -> FaceTemplateModel:
    settings = get_settings()
    if not settings.biometric_matching_enabled:
        raise HTTPException(
            status_code=409,
            detail="biometric matching is disabled "
                   "(USALI_BIOMETRIC_MATCHING_ENABLED); enrollment would "
                   "compute a face template the flag says is not processed",
        )
    with _session(request) as session:
        employee = _load_scoped_employee(session, employee_id, principal)
        _refuse_unencoded_jurisdictions(session, employee_id)
        if employee.employment_status == "terminated":
            raise HTTPException(
                status_code=409,
                detail="employee is terminated; separation deletes face "
                       "templates rather than creating them",
            )

        data = photo.file.read(_MAX_PHOTO_BYTES + 1)
        if len(data) > _MAX_PHOTO_BYTES:
            raise HTTPException(status_code=413, detail="photo too large")
        if not data.startswith(JPEG_MAGIC):
            raise HTTPException(status_code=422, detail="photo must be a JPEG")

        notice = settings.biometric_notice_version.strip() or None
        engine = _engine(request)
        try:
            action = write_face_template(
                session, engine, _photo_store(request),
                employee_id=employee_id, photo_bytes=data,
                actor_subject=principal.subject, notice_version=notice,
            )
        except FaceModelsMissing as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if action is None:
            raise HTTPException(
                status_code=422,
                detail="no face detected in the photo; retake with the face "
                       "clearly visible",
            )
        session.commit()
        return FaceTemplateModel(
            employee_id=employee_id,
            model_version=engine.model_version,
            notice_version=notice,
        )


@router.get("/employees/{employee_id}/face-template")
def face_template_status(
    employee_id: int, request: Request,
    principal: Principal = Depends(require_onboarder),
) -> FaceTemplateStatusModel:
    """Enrollment status for the Employees page. The embedding NEVER crosses
    the wire — there is no read path for it, by design."""
    with _session(request) as session:
        _load_scoped_employee(session, employee_id, principal)
        row = session.execute(
            select(EmployeeFaceTemplate).where(
                EmployeeFaceTemplate.employee_id == employee_id
            )
        ).scalar_one_or_none()
        if row is None:
            return FaceTemplateStatusModel(employee_id=employee_id, enrolled=False)
        return FaceTemplateStatusModel(
            employee_id=employee_id, enrolled=True,
            model_version=row.model_version,
            notice_version=row.notice_version,
            created_at=row.created_at.isoformat(),
        )


@router.delete("/employees/{employee_id}/face-template", status_code=204)
def delete_face_template(
    employee_id: int, request: Request,
    principal: Principal = Depends(require_onboarder),
) -> None:
    """Remove the template and reference photo (consent withdrawal, bad
    capture). Deleting nothing is a 404, not a silent success — the caller
    should know the record they meant to remove wasn't there."""
    with _session(request) as session:
        _load_scoped_employee(session, employee_id, principal)
        row = session.execute(
            select(EmployeeFaceTemplate).where(
                EmployeeFaceTemplate.employee_id == employee_id
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="no face template enrolled")
        session.delete(row)
        _photo_store(request).delete(
            face_reference_key(current_org_id(session), employee_id)
        )
        session.add(AuditEvent(
            actor_subject=principal.subject, action="face_template_deleted",
            resource_type="employee", resource_id=str(employee_id),
        ))
        session.commit()
