"""Night-audit endpoints: checklist state, gated upload, and the date roll.

Auth mirrors property_config_api: reads gate on `_require_readable_property`;
upload and roll gate on `require_grants(ORG_ADMIN, PROPERTY_GM)` composed with
`_require_onboardable_property` (org_admin bypass; a GM confined to assigned
properties). The roll emits one AuditEvent; uploads are audited by their
IngestBatch rows, as every ingest already is.

The upload VALIDATES BEFORE it ingests: the PDF is parsed once up front to check
it detects as this property, as one of the night's required report types, and as
the CURRENT business date — a mismatched file is refused with nothing staged
(the generic /ingest stays unrestricted for backfills and corrections). Only a
valid file reaches `process_file`, which owns staging, transform, coverage, and
filing exactly as it does for every other ingest path.
"""

import re
from pathlib import Path
from datetime import UTC, datetime, timedelta

from collections.abc import Callable
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.adaptors import autoclerk_manager_report as mgr
from usali.adaptors import autoclerk_rate_plan as rate_plan
from usali.adaptors import autoclerk_transaction_summary as autoclerk
from usali.adaptors import opera_trial_balance as opera
from usali.adaptors import skytouch_hotel_journal as sky_journal
from usali.adaptors import skytouch_hotel_statistics as sky_stats
from usali.adaptors.pack import split_pack
from usali.adaptors.pdf import extract_pages, extract_words
from usali.auth import (
    ORG_ADMIN,
    PROPERTY_GM,
    Principal,
    request_session_factory,
    require_grants,
    require_operator,
)
from usali.detect import Detection, detect, load_registry
from usali.ingestion import ProcessingError, process_file, process_pack
from usali.segment_promote import promote_segments
from sqlalchemy import delete
from usali.models import (
    AuditEvent,
    NightAuditAdjustment,
    NightAuditState,
    PmsDailySegmentStage,
    Property,
    UsaliLedgerBalanceFact,
    UsaliSegmentFact,
)
from usali.night_audit import (
    PACK_UPLOAD,
    get_or_init_state,
    ledger_checks,
    roll_window,
    segment_reconciliation,
    slot_status,
)
from usali.workforce import (
    _require_onboardable_property,
    _require_readable_property,
    resolve_scope,
)

require_auditor = require_grants(ORG_ADMIN, PROPERTY_GM)

router = APIRouter(prefix="/api/properties")

_MAX_PDF_BYTES = 25 * 1024 * 1024

# (pms_source, report_type) -> the adaptor's own business-date extractor, for
# the pre-ingest date check. Mirrors ingestion._PIPELINES' date derivation.
_DATE_FNS = {
    ("OPERA", "trial_balance"): opera.extract_business_date,
    ("OPERA", "manager_flash"): opera.extract_business_date,
    ("OPERA", "market_stats"): opera.extract_business_date,
    ("AUTOCLERK", "transaction_summary"): autoclerk.extract_business_date,
    ("AUTOCLERK", "manager_report"): mgr.extract_business_date,
    ("AUTOCLERK", "rate_plan"): rate_plan.extract_business_date,
    ("SKYTOUCH", "hotel_journal"): sky_journal.extract_business_date,
    ("SKYTOUCH", "hotel_statistics"): sky_stats.extract_business_date,
}


def _session(request: Request) -> Session:
    return request_session_factory(request)()


def _get_property(session: Session, property_id: str) -> Property:
    prop = session.get(Property, property_id)
    if prop is None:
        raise HTTPException(status_code=403, detail="property out of scope")
    return prop


def _state_payload(session: Session, prop: Property) -> dict[str, object]:
    state = get_or_init_state(session, prop)
    day = state.current_business_date
    slots = slot_status(session, prop.property_id, day, prop.pms_source)
    checks = ledger_checks(session, prop.property_id, day)
    window = roll_window(prop)
    segments = segment_reconciliation(session, prop.property_id, day, prop.pms_source)
    all_landed = bool(slots) and all(bool(s["landed"]) for s in slots)
    any_failed = any(c.status == "fail" for c in checks) or (
        segments is not None and segments["status"] == "fail"
    )
    pack_label = PACK_UPLOAD.get(prop.pms_source.upper())
    return {
        "property_id": prop.property_id,
        "pms_source": prop.pms_source,
        "upload_mode": "pack" if pack_label is not None else "reports",
        "pack_label": pack_label,
        "business_date": day.isoformat(),
        "closed_through": (day - timedelta(days=1)).isoformat(),
        "slots": slots,
        "verification": [
            {"name": c.name, "status": c.status, "detail": c.detail,
             "delta": c.delta, "adjust": c.adjust}
            for c in checks
        ],
        "segments": segments,
        "window": window,
        "all_reports_landed": all_landed,
        "can_roll": all_landed and not any_failed and bool(window["open"]),
        "last_rolled_at": (
            state.last_rolled_at.isoformat() if state.last_rolled_at else None
        ),
    }


@router.get("/{property_id}/night-audit")
def get_night_audit(
    property_id: str, request: Request,
    principal: Principal = Depends(require_operator),
) -> dict[str, object]:
    with _session(request) as session:
        _require_readable_property(session, resolve_scope(principal, session), property_id)
        prop = _get_property(session, property_id)
        payload = _state_payload(session, prop)
        session.commit()  # persists a lazily-created state row
        return payload


@router.post("/{property_id}/night-audit/upload", status_code=201)
async def upload_night_audit_report(
    property_id: str, request: Request, file: UploadFile,
    principal: Principal = Depends(require_auditor),
) -> dict[str, object]:
    upload_name = file.filename or "upload.pdf"
    # Multipart filenames are attacker-controlled (the /ingest rule): display
    # only, never a path component.
    if (
        upload_name in {".", ".."}
        or "/" in upload_name or "\\" in upload_name or "\x00" in upload_name
    ):
        raise HTTPException(status_code=422, detail="unsafe upload filename")
    payload = await file.read(_MAX_PDF_BYTES + 1)
    if len(payload) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF too large")

    inbox, processed, failed = request.app.state.ingest_dirs
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        prop = _get_property(session, property_id)
        state = get_or_init_state(session, prop)

        inbox.mkdir(parents=True, exist_ok=True)
        # Prefix with the property + date so a night's re-send can't collide
        # with another property's same-named export.
        dest = inbox / f"night-audit-{property_id}-{re.sub(r'[^A-Za-z0-9._-]', '_', upload_name)}"
        try:
            with dest.open("xb") as staged:
                staged.write(payload)
        except FileExistsError as exc:
            raise HTTPException(
                status_code=409, detail="an upload with that filename is pending"
            ) from exc

        def _refuse(status: int, detail: str) -> HTTPException:
            dest.unlink(missing_ok=True)  # nothing staged — leave no orphan
            return HTTPException(status_code=status, detail=detail)

        if PACK_UPLOAD.get(prop.pms_source.upper()) is not None:
            return _ingest_pack(session, request, prop, state, dest, _refuse)

        # -- Pre-ingest validation: right property, right report, right day. --
        try:
            words = extract_words(dest)
            det = detect(words, load_registry(session))
        except Exception as exc:
            raise _refuse(422, f"could not read report: {exc}") from exc
        if det.property_id != property_id:
            raise _refuse(
                422, f"report is for property {det.property_id}, not {property_id}"
            )
        required = {str(s["report_type"]) for s in
                    slot_status(session, property_id, state.current_business_date, prop.pms_source)}
        if det.report_type not in required:
            raise _refuse(
                422,
                f"{det.report_type} is not part of this property's night audit "
                f"({prop.pms_source} requires: {', '.join(sorted(required))})",
            )
        date_fn = _DATE_FNS.get((det.pms_source, det.report_type))
        if date_fn is not None:
            try:
                report_date = date_fn(words)
            except Exception as exc:
                raise _refuse(422, f"could not read the report's business date: {exc}") from exc
            if report_date != state.current_business_date:
                raise _refuse(
                    422,
                    f"report is for {report_date.isoformat()} but the current "
                    f"business date is {state.current_business_date.isoformat()} — "
                    "use the Upload page for backfills",
                )

        try:
            r = process_file(session, dest, processed_dir=processed, failed_dir=failed)
        except ProcessingError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        prop = _get_property(session, property_id)  # re-fetch post-commit
        return {
            "report_type": r.report_type,
            "business_date": r.business_date.isoformat(),
            "staged": r.staged,
            "mapped": r.mapped,
            "unmapped": r.unmapped,
            # One-element sections list: the pack and single-report responses
            # share a shape, so the page renders both with one component.
            "sections": [{"title": r.report_type.replace("_", " ").title(),
                          "report_type": r.report_type, "staged": r.staged,
                          "mapped": r.mapped, "skipped": False}],
            **_state_payload(session, prop),
        }


def _ingest_pack(
    session: Session, request: Request, prop: Property, state: NightAuditState,
    dest: Path, _refuse: Callable[[int, str], HTTPException],
) -> dict[str, object]:
    """Split the pack, validate every RECOGNIZED section (right property, right
    business date) BEFORE anything stages, then hand the file to process_pack —
    which owns the shared transaction, per-section coverage, quarantine, and
    filing. The response names every section so the auditor sees exactly what
    the pack contained and what was skipped."""
    try:
        pages = extract_pages(dest)
        sections = split_pack(pages)
    except Exception as exc:
        raise _refuse(422, f"could not read the pack: {exc}") from exc
    registry = load_registry(session)
    recognized: list[tuple[str, Detection]] = []
    skipped_titles: list[str] = []
    for section in sections:
        try:
            det = detect(section.words, registry)
        except ValueError:
            skipped_titles.append(section.title or "(untitled)")
            continue
        recognized.append((section.title, det))
        if det.property_id != prop.property_id:
            raise _refuse(
                422,
                f"pack section {section.title!r} is for property "
                f"{det.property_id}, not {prop.property_id}",
            )
        date_fn = _DATE_FNS.get((det.pms_source, det.report_type))
        if date_fn is None:
            continue
        try:
            report_date = date_fn(section.words)
        except Exception as exc:
            raise _refuse(
                422, f"could not read the business date of {section.title!r}: {exc}"
            ) from exc
        if report_date != state.current_business_date:
            raise _refuse(
                422,
                f"pack section {section.title!r} is for {report_date.isoformat()} "
                f"but the current business date is "
                f"{state.current_business_date.isoformat()} — use the Upload "
                "page for backfills",
            )
    if not recognized:
        raise _refuse(422, "no recognized report sections in this pack")

    _, processed, failed = request.app.state.ingest_dirs
    try:
        results = process_pack(session, dest, processed_dir=processed, failed_dir=failed)
    except ProcessingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    by_type = {r.report_type: r for r in results}
    section_rows = []
    for title, det in recognized:
        r = by_type.get(det.report_type)
        section_rows.append({
            "title": title, "report_type": det.report_type,
            "staged": r.staged if r else 0, "mapped": r.mapped if r else 0,
            "skipped": r is None,
        })
    for title in skipped_titles:
        section_rows.append({"title": title, "report_type": None,
                             "staged": 0, "mapped": 0, "skipped": True})
    fresh = _get_property(session, prop.property_id)  # re-fetch post-commit
    return {"sections": section_rows, **_state_payload(session, fresh)}


class AdjustBody(BaseModel):
    corrected_amount: Decimal
    reason: str = Field(min_length=3, max_length=300)

    @field_validator("corrected_amount")
    @classmethod
    def _two_dp(cls, v: Decimal) -> Decimal:
        return v.quantize(Decimal("0.01"))


@router.post("/{property_id}/night-audit/adjust")
def adjust_prior_close(
    property_id: str, body: AdjustBody, request: Request,
    principal: Principal = Depends(require_auditor),
) -> dict[str, object]:
    """Correct the PRIOR business date's AR close directly — the one hole a
    re-upload cannot fix, because the PMS export is what it is. The stored
    ledger-balance FACT is edited in place; the stage row keeps the PMS-said
    value, and every correction lands in night_audit_adjustment (old → new,
    mandatory reason, actor) plus an AuditEvent. Scope is deliberately narrow:
    only the cross-night check's input is editable here — an identity failure
    means tonight's own report contradicts itself and needs a corrected export.
    """
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        prop = _get_property(session, property_id)
        state = get_or_init_state(session, prop)
        prior_day = state.current_business_date - timedelta(days=1)

        fact = session.execute(
            select(UsaliLedgerBalanceFact).where(
                UsaliLedgerBalanceFact.property_id == property_id,
                UsaliLedgerBalanceFact.business_date == prior_day,
                UsaliLedgerBalanceFact.ledger_code == "AR_LEDGER",
                UsaliLedgerBalanceFact.kind == "balance",
            )
        ).scalar_one_or_none()
        if fact is None:
            raise HTTPException(
                status_code=409,
                detail=f"no AR close on file for {prior_day.isoformat()} — "
                "there is nothing to correct (the roll-forward check is skipped, "
                "not failing)",
            )
        old_amount = Decimal(str(fact.amount))
        fact.amount = body.corrected_amount  # type: ignore[assignment]  # Numeric column; Decimal adapts exactly
        session.add(NightAuditAdjustment(
            property_id=property_id, business_date=prior_day,
            ledger_code="AR_LEDGER", old_amount=old_amount,
            new_amount=body.corrected_amount, reason=body.reason.strip(),
            actor_subject=principal.subject,
        ))
        session.add(AuditEvent(
            actor_subject=principal.subject, action="night_audit_balance_adjusted",
            resource_type="property", resource_id=property_id,
        ))
        session.commit()
        return _state_payload(session, prop)


class SegmentRowBody(BaseModel):
    code: str
    rooms: Decimal
    room_revenue: Decimal

    @field_validator("rooms")
    @classmethod
    def _whole_rooms(cls, v: Decimal) -> Decimal:
        # Rooms are counts — refuse fractional entries at the boundary.
        if v != v.to_integral_value():
            raise ValueError("rooms must be a whole number")
        return v

    @field_validator("room_revenue")
    @classmethod
    def _two_dp(cls, v: Decimal) -> Decimal:
        return v.quantize(Decimal("0.01"))


class SegmentsBody(BaseModel):
    rows: list[SegmentRowBody] = Field(min_length=1)


@router.post("/{property_id}/night-audit/segments")
def save_segment_rows(
    property_id: str, body: SegmentsBody, request: Request,
    principal: Principal = Depends(require_auditor),
) -> dict[str, object]:
    """Direct-edit the CURRENT date's RAW market-code rows (the report's own
    lines) so the table ties to the Manager Flash / Trial Balance references.

    The corrected table IS the corrected report: the staged per-code values are
    updated, the report's own TOTAL row is moved to the new sums (promotion is
    strict about Σ codes == TOTAL), the old USALI rollup facts for the date are
    dropped, and `segment_promote` rebuilds them through its own strict path —
    the accounting view can never drift from the corrected report. Only
    existing codes may be edited; the table is not a place to invent segments.
    Every changed value lands in night_audit_adjustment (old -> new, auto-
    stamped reason, actor) plus one AuditEvent — no reason is asked, by design.
    """
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        prop = _get_property(session, property_id)
        state = get_or_init_state(session, prop)
        day = state.current_business_date

        stage_rows = session.execute(
            select(PmsDailySegmentStage).where(
                PmsDailySegmentStage.property_id == property_id,
                PmsDailySegmentStage.business_date == day,
                PmsDailySegmentStage.period_label == "DAY",
            )
        ).scalars().all()
        if not stage_rows:
            raise HTTPException(
                status_code=409,
                detail="no market-code rows on file for "
                f"{day.isoformat()} — upload Market Code Statistics first",
            )
        by_key = {(r.segment_code, r.measure): r for r in stage_rows}
        known_codes = {r.segment_code for r in stage_rows if r.segment_code != "TOTAL"}
        unknown = [r.code for r in body.rows if r.code not in known_codes]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"unknown market codes: {', '.join(sorted(unknown))}",
            )

        changed = 0

        def _set(code: str, measure: str, new_value: Decimal) -> None:
            nonlocal changed
            row = by_key.get((code, measure))
            if row is None:
                return
            old_value = Decimal(str(row.value))
            if old_value == new_value:
                return
            row.value = new_value  # type: ignore[assignment]  # Numeric column; Decimal adapts exactly
            session.add(NightAuditAdjustment(
                property_id=property_id, business_date=day,
                ledger_code=f"MKT:{code}:{measure}",
                old_amount=old_value, new_amount=new_value,
                reason="market-code reconciliation edit",
                actor_subject=principal.subject,
            ))
            changed += 1

        for row in body.rows:
            _set(row.code, "ROOMS", row.rooms)
            _set(row.code, "ROOM_REVENUE", row.room_revenue)

        # The TOTAL row follows the corrected sums — promotion refuses otherwise.
        session.flush()
        for measure in ("ROOMS", "ROOM_REVENUE"):
            total = sum(
                (Decimal(str(r.value)) for r in stage_rows
                 if r.segment_code != "TOTAL" and r.measure == measure),
                Decimal("0"),
            )
            _set("TOTAL", measure, total)

        if changed:
            # Rebuild the USALI rollup from the corrected stage through the
            # strict promote path (delete-then-repromote; promote skips when
            # facts exist).
            session.execute(
                delete(UsaliSegmentFact).where(
                    UsaliSegmentFact.property_id == property_id,
                    UsaliSegmentFact.business_date == day,
                    UsaliSegmentFact.pms_source == prop.pms_source,
                )
            )
            session.flush()
            promote_segments(
                session, "mapping/segments.yaml",
                source=prop.pms_source, business_date=day,
            )
            session.add(AuditEvent(
                actor_subject=principal.subject,
                action="night_audit_segments_adjusted",
                resource_type="property", resource_id=property_id,
            ))
        session.commit()
        return _state_payload(session, prop)


@router.post("/{property_id}/night-audit/roll")
def roll_night_audit(
    property_id: str, request: Request,
    principal: Principal = Depends(require_auditor),
) -> dict[str, object]:
    with _session(request) as session:
        _require_onboardable_property(session, principal, property_id)
        prop = _get_property(session, property_id)
        state = get_or_init_state(session, prop)
        day = state.current_business_date

        slots = slot_status(session, property_id, day, prop.pms_source)
        missing = [str(s["label"]) for s in slots if not s["landed"]]
        if not slots:
            raise HTTPException(
                status_code=409,
                detail=f"no night-audit report set is defined for {prop.pms_source}",
            )
        if missing:
            raise HTTPException(
                status_code=409,
                detail=f"cannot roll: still awaiting {', '.join(missing)} for {day.isoformat()}",
            )
        failed = [c for c in ledger_checks(session, property_id, day) if c.status == "fail"]
        if failed:
            raise HTTPException(
                status_code=409,
                detail="cannot roll: ledger checks failed — "
                + "; ".join(f"{c.name} (Δ {c.delta})" for c in failed),
            )
        segments = segment_reconciliation(session, property_id, day, prop.pms_source)
        if segments is not None and segments["status"] == "fail":
            raise HTTPException(
                status_code=409,
                detail=(
                    "cannot roll: market-code reconciliation does not tie — "
                    f"rooms Δ {segments['rooms_delta']}, revenue Δ {segments['revenue_delta']}"
                ),
            )
        window = roll_window(prop)
        if not window["open"]:
            raise HTTPException(
                status_code=409,
                detail=f"the roll window is {window['hours']} {prop.timezone} "
                f"(it is {window['local_time']} at the property now)",
            )

        state.current_business_date = day + timedelta(days=1)
        state.last_rolled_at = datetime.now(UTC)
        session.add(AuditEvent(
            actor_subject=principal.subject, action="night_audit_rolled",
            resource_type="property", resource_id=property_id,
        ))
        session.commit()
        return _state_payload(session, prop)
