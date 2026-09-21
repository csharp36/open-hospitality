"""The emailed night-audit webhook: `POST /api/intake/email` (OH-23).

The Cloudflare Email Worker (D-OH23.1) forwards a whole RFC822 message here as
`message/rfc822` with five headers: `X-Intake-Timestamp`,
`X-Intake-Signature`, `X-Intake-To`, `X-Intake-From` and `X-Intake-Auth`. This
module turns that into at most one `email_intake_event` row and whatever
ingesting its attachments produces.

WHAT THE STATUS CODE MEANS. The worker forwards the message to the fallback
mailbox on any non-2xx, and that mailbox holds unredacted mail outside the
gate, so a non-2xx is reserved for "this service did not dispose of the
message": a bad signature or a stale timestamp (401), an over-large body
(413), a flood (429). Every disposition the service DID make — an unknown
address, a rejected sender, an attachment that failed to parse — is a 200
carrying the outcome, because the message has been dealt with and re-mailing
it to a human achieves nothing (D-OH23.9).

There is one place that rule does not hold, and it is named rather than
fixed: the event is committed LAST, so a failure inside `_record` — after the
attachments have already been ingested and committed — answers 5xx for a
message this service DID dispose of. The worker then holds it in the fallback
mailbox, and a re-delivery finds the batches already there and records
`duplicate`. The cost is one missing line in the event log, not a lost report.

WHERE THE ORG COMES FROM. `property_intake_address` is not OrgScoped and
carries no RLS policy, so the local-part lookup runs on the UNBOUND base
factory — it must, since no org is known until the row is read. Every write
after it, the event row included, happens inside
`OrgBoundSessionFactory(base, row.org_id)`. That is not a nicety:
`email_intake_event.org_id` carries the OrgScoped server default of 1, so an
event written on an unbound session lands in the founding org whatever
address it names. tests/test_intake_email.py::
test_an_address_on_another_orgs_property_ingests_into_that_org is the pin.

WHAT RUNS ON THE EVENT LOOP. Reading the attachments is synchronous CPU work
done inline, as `/ingest` and the night-audit upload do it, so a message with
several large PDFs holds the loop for the length of the parse. `/api/preview`
is the one endpoint that moves that work to a thread (`anyio.to_thread`), and
it does so because it is anonymous; this route is bounded instead by the HMAC,
the body cap, `_MAX_ATTACHMENTS` and the rate limiter. If the pilot outgrows
that, the fix is the preview's, applied to the block below `_read_body`.

WHAT THIS ROUTE DOES NOT PRODUCE. `unreadable` is in the event table's closed
set, but no attachment here is given it. The validator answers `unreadable`
for bytes it could not read at all, which is a FAILURE rather than a judgment
about the property or the report type — and D-OH23.9 makes the gate the place
a failure is recorded, as a `failed` IngestBatch plus an error record. So an
unreadable attachment is handed to `process_document_bytes` like any other and
comes back `failed`; see `_ingest_attachment`.
"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from email import message_from_bytes, policy
from pathlib import Path
from typing import Literal, cast, get_args

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import String, select
from sqlalchemy.orm import Session

from usali.config import get_settings
from usali.ingestion import ProcessingError, process_document_bytes
from usali.intake import attachments_of, local_part, safe_component, sender_allowed
from usali.intake import verify_signature
from usali.models import (
    EmailIntakeEvent,
    IngestBatch,
    Property,
    PropertyIntakeAddress,
)
from usali.night_audit_api import _MAX_UPLOAD_BYTES
from usali.night_audit_validation import validate_for_property
from usali.ratelimit import RateLimiter
from usali.redaction import mask_pans
from usali.tenancy import OrgBoundSessionFactory, SessionFactory

router = APIRouter(prefix="/api/intake")

# D-OH23.7's closed set, as a type. `intake.INTAKE_OUTCOMES` is a frozenset,
# which mypy cannot use to refuse a stray string at a call site; this Literal
# can, and tests/test_intake_email.py::
# test_the_literal_outcomes_match_the_event_tables_closed_set holds the two
# spellings equal (and that test's other leg, in tests/test_intake_schema.py,
# holds the frozenset to the CHECK).
IntakeOutcome = Literal[
    "ingested",
    "partial",
    "duplicate",
    "no_attachment",
    "sender_rejected",
    "wrong_property",
    "not_a_night_audit_report",
    "unreadable",
    "failed",
    "revoked_address",
]
INTAKE_OUTCOME_NAMES: frozenset[str] = frozenset(get_args(IntakeOutcome))

# A RESPONSE outcome only: a message to a local part no row answers to has no
# org to record it under, so nothing is stored (D-OH23.7). It is deliberately
# absent from IntakeOutcome above, which is what the CHECK accepts.
UNKNOWN_ADDRESS = "unknown_address"

# What one attachment can come back as. Narrower than IntakeOutcome: the
# message-level outcomes are not attachment outcomes, and `unreadable` is not
# produced here (see the module docstring).
AttachmentOutcome = Literal[
    "ingested", "duplicate", "wrong_property", "not_a_night_audit_report", "failed"
]

# The two validator outcomes that are JUDGMENTS about the document rather than
# a failure to read it. These refuse the attachment where `unreadable` does
# not; `_ingest_attachment` is where that split is made.
_VALIDATION_REFUSALS: tuple[str, ...] = ("wrong_property", "not_a_night_audit_report")

# Per-attachment size ceiling: the upload endpoint's own constant, imported
# rather than restated so the two surfaces cannot drift apart. Pinned by
# tests/test_intake_email.py::test_an_attachment_is_capped_at_the_upload_endpoints_limit.
# With both caps at 25 MB it is unreachable in practice: MIME base64-encodes a
# part at 4/3, so a 25 MB attachment needs a ~33 MB body and the body cap
# refuses that first. It starts to bite only if USALI_EMAIL_INTAKE_MAX_BYTES is
# raised, which is exactly when a per-attachment ceiling should still hold.
_MAX_ATTACHMENT_BYTES = _MAX_UPLOAD_BYTES

# How many attachments of one message are opened. A night's exports are four
# files at the most any PMS here sends (usali.night_audit.REQUIRED_REPORTS is
# where the per-source lists live); ten leaves room for a signature image or a
# forwarded copy.
#
# It bounds ENUMERATION and not only ingestion: it is passed to
# `attachments_of` as its `limit`, so the walk stops at eleven kept parts and
# the surplus is never decoded, hashed or given an entry of its own. Counting
# them first would cost seconds of blocked event loop and megabytes of
# response and JSONB for a message built to do exactly that. Everything past
# the ceiling becomes the single `_SURPLUS_ENTRY` below.
_MAX_ATTACHMENTS = 10

# What stands in for every attachment past `_MAX_ATTACHMENTS`: one entry, not
# one per part. It is built literally rather than through `_entry` because it
# describes no part — there are no bytes to size, no content to hash, and the
# name is this module's own constant rather than author-chosen text, so there
# is nothing in it to mask.
_SURPLUS_ENTRY: dict[str, object] = {
    "name": "(more attachments)",
    "sha256": "",
    "bytes": 0,
    "outcome": "failed",
    "error": "too_many_attachments",
}

# Error text stored in the event's JSON and returned to the worker. The same
# bound ingestion._safe_message puts on a batch message.
_ERROR_MAX = 500

# One worker posts here, so the limiter is keyed on a constant: it is a
# ceiling on the ROUTE, not a per-client budget, and a spoofable client IP
# cannot buy a second one.
_RATE_LIMIT_KEY = "intake-webhook"


def _clip(column: str, value: str) -> str:
    """Clip `value` to the width `EmailIntakeEvent` declares for `column`.

    Postgres raises StringDataRightTruncation on overflow rather than
    truncating, so every header that reaches a String column is clipped before
    the INSERT. The width is read off the model instead of copied, so widening
    a column here needs no second edit.
    """
    width = cast(String, EmailIntakeEvent.__table__.c[column].type).length
    return value if width is None else value[:width]


def _subject_and_message_id(raw: bytes) -> tuple[str, str]:
    """The two MIME headers the event keeps, or empty strings.

    Never raises: an unparseable message still gets an event row, exactly as
    `intake.attachments_of` still answers an empty attachment list for one.
    """
    try:
        message = message_from_bytes(raw, policy=policy.default)
        return str(message.get("Subject", "")), str(message.get("Message-ID", ""))
    except Exception:
        return "", ""


def _stored(column: str, value: str) -> str:
    """The one rule for every free-text header `email_intake_event` keeps.

    Clipped to the column's width first, so `mask_pans` runs over bounded
    text; masked; then clipped again, so whatever masking returns still fits
    the column. D-OH23.7 requires the masking and `models.EmailIntakeEvent`
    the width — every one of the four string columns goes through here, not
    just the subject: an envelope sender, an Authentication-Results comment
    and a Message-ID are all attacker-chosen text that a card number fits in.
    """
    return _clip(column, mask_pans(_clip(column, value)))


def _masked(text: str) -> str:
    return mask_pans(text)[:_ERROR_MAX]


def _transformed_batch_ids(session: Session, sha256: str) -> list[int]:
    """The `transformed` batches this org already holds for these bytes.

    `IngestBatch.file_hash` is the sha256 hexdigest of the whole file
    (`ingestion._hash`), which is what makes the comparison below meaningful;
    tests/test_intake_email.py::test_the_same_message_again_is_a_duplicate is
    what fails if the two ever spell the digest differently. The session is
    org-bound, so both the dedupe read (D-OH23.6) and the batch ids handed
    back to the worker stay inside the address's tenant.
    """
    return list(
        session.scalars(
            select(IngestBatch.batch_id)
            .where(IngestBatch.file_hash == sha256, IngestBatch.status == "transformed")
            .order_by(IngestBatch.batch_id)
        )
    )


def _safe_name(name: str) -> str:
    """The one spelling of an attachment's name, everywhere it is kept.

    An attachment name is author-chosen text that ends up in three places a
    card number must not reach: the event's `attachments` JSON, the
    `source_file` an ingest records, and the stem of the filed artifact or
    error record. So the digits are masked FIRST and the result reduced to the
    artifact-stem alphabet second — `4111111111111111.pdf` becomes
    `_____1111.pdf`, and the sha256 beside it in every entry is the identity
    the name never was. Applying this twice changes nothing, which is what
    lets `_entry` apply it to whatever it is handed.
    """
    return safe_component(mask_pans(name))


def _entry(
    name: str, data: bytes, sha256: str, outcome: AttachmentOutcome, **extra: object
) -> dict[str, object]:
    """One element of the event's `attachments` JSON, in D-OH23.7's shape:
    `{name, sha256, bytes, outcome, batch_id?, error?}`. The response hands the
    worker the same objects the event stores.

    The name is masked here rather than at the one call site that ingests, so
    that the `too_many_attachments` entry — which never reaches an ingest —
    is covered by the same rule. Pinned by tests/test_intake_email.py::
    test_event_text_carries_no_card_numbers.
    """
    return {"name": _safe_name(name), "sha256": sha256, "bytes": len(data),
            "outcome": outcome, **extra}


def _ingest_attachment(
    session: Session,
    data: bytes,
    name: str,
    *,
    property_id: str,
    pms_source: str,
    processed_dir: Path,
    failed_dir: Path,
) -> dict[str, object]:
    """Dedupe, validate, ingest — one attachment, one entry.

    The validator's two JUDGMENTS refuse the attachment with nothing staged
    (D-OH23.5). `unreadable` does not: it means the bytes could not be read,
    and a failure is the gate's to record — handing them to
    `process_document_bytes` is what leaves the `failed` IngestBatch and the
    error record D-OH23.9 calls the trace, so such an attachment comes back
    `failed`. Both paths read through `adaptors.reader.read_words_from_bytes`
    and route the same way, which
    tests/test_night_audit_validation.py::test_routing_agrees_with_the_ingest_path
    pins; the guard below is what happens if they ever disagree, and it
    refuses to call such an ingest `ingested`.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    if len(data) > _MAX_ATTACHMENT_BYTES:
        return _entry(name, data, sha256, "failed",
                      error=f"attachment is too large ({len(data)} bytes)")
    if _transformed_batch_ids(session, sha256):
        return _entry(name, data, sha256, "duplicate")

    verdict = validate_for_property(session, data, property_id, pms_source)
    if verdict in _VALIDATION_REFUSALS:
        # The two judgments, and only those: `verdict` is narrowed by the
        # tuple above, which holds the same two names AttachmentOutcome does.
        return _entry(name, data, sha256, cast(AttachmentOutcome, verdict))

    try:
        process_document_bytes(
            session, data, name, processed_dir=processed_dir, failed_dir=failed_dir
        )
    except ProcessingError as exc:
        return _entry(name, data, sha256, "failed", error=_masked(str(exc)))
    if verdict is not None:
        raise RuntimeError(
            f"{name}: the validator answered {verdict} but the gate ingested it "
            "— the two readers diverged (their agreement is pinned by "
            "tests/test_night_audit_validation.py::"
            "test_routing_agrees_with_the_ingest_path)"
        )
    batch_ids = _transformed_batch_ids(session, sha256)
    # One attachment may stage several batches — a pack stages one per
    # recognized section. D-OH23.7's entry carries a single `batch_id`, so it
    # names the first; the sha256 beside it is what finds the others.
    return _entry(name, data, sha256, "ingested",
                  batch_id=batch_ids[0] if batch_ids else None)


def _attachment_entries(
    session: Session,
    raw: bytes,
    *,
    property_id: str,
    pms_source: str,
    processed_dir: Path,
    failed_dir: Path,
) -> list[dict[str, object]]:
    """One entry per attachment, in the order the message carried them, plus
    at most one `_SURPLUS_ENTRY` standing for everything past the ceiling.

    `attachments_of` is asked for `_MAX_ATTACHMENTS + 1` and stops there, so
    a message of many parts costs the ceiling rather than its part count, and
    the surplus is one entry rather than one per part. Pinned by
    tests/test_intake_email.py::test_attachments_past_the_cap_are_recorded_and_not_processed
    and ::test_a_message_of_many_parts_is_bounded_by_the_ceiling.
    """
    found = attachments_of(raw, limit=_MAX_ATTACHMENTS)
    entries: list[dict[str, object]] = [
        # The same `_safe_name` the entry stores, so the name that reaches
        # `IngestBatch.source_file` and the filed artifact's stem is the one
        # the event log shows — and carries no card number either.
        _ingest_attachment(
            session, data, _safe_name(name), property_id=property_id,
            pms_source=pms_source, processed_dir=processed_dir,
            failed_dir=failed_dir,
        )
        for name, data in found[:_MAX_ATTACHMENTS]
    ]
    if len(found) > _MAX_ATTACHMENTS:
        entries.append(dict(_SURPLUS_ENTRY))
    return entries


def _aggregate(entries: Sequence[dict[str, object]]) -> IntakeOutcome:
    """The message's outcome, from its attachments'.

    All ingested is `ingested`; some ingested and some not is `partial`. When
    nothing ingested, a `failed` attachment outranks every other refusal, and
    only then does the FIRST refusal stand. The ranking is there because a
    message whose outcome read `duplicate` — a re-sent report beside one that
    would not parse — buries the half an operator has to act on, and the
    event log's outcome column is the first thing they read. An empty list
    cannot reach here; `receive_email` answers `no_attachment` before calling
    it. Pinned by tests/test_intake_email.py::
    test_a_failure_outranks_a_duplicate_when_nothing_ingested.
    """
    outcomes = [str(entry["outcome"]) for entry in entries]
    if all(outcome == "ingested" for outcome in outcomes):
        return "ingested"
    if any(outcome == "ingested" for outcome in outcomes):
        return "partial"
    if any(outcome == "failed" for outcome in outcomes):
        return "failed"
    return cast(IntakeOutcome, outcomes[0])


def _record(
    session: Session,
    *,
    address_id: int,
    property_id: str,
    envelope_from: str,
    auth_result: str,
    subject: str,
    message_id: str,
    outcome: IntakeOutcome,
    entries: Sequence[dict[str, object]],
) -> None:
    """Write the event and commit it in its own transaction.

    Called LAST, after every attachment has been disposed of. A ProcessingError
    from the gate has already rolled its session back and committed the failed
    batch, so the event written here is what ties that batch to the message
    that carried it; committing the event earlier would put it at the mercy of
    that rollback.
    """
    session.add(EmailIntakeEvent(
        address_id=address_id,
        property_id=property_id,
        envelope_from=_stored("envelope_from", envelope_from),
        subject=_stored("subject", subject) or None,
        auth_result=_stored("auth_result", auth_result) or None,
        message_id=_stored("message_id", message_id) or None,
        outcome=outcome,
        attachments=list(entries),
    ))
    session.commit()


def _resolve_address(
    factory: SessionFactory, envelope_to: str, domain: str
) -> tuple[int, int, str, list[str] | None, bool] | None:
    """(address_id, org_id, property_id, sender_domains, revoked) or None.

    Runs on the UNBOUND base factory: no org is known until this row is read,
    and `property_intake_address` carries no RLS policy precisely so that this
    lookup is possible (the o1a0intake migration records why).

    `intake.local_part` ignores the domain, so the domain is compared here; a
    message addressed to the right local part at somebody else's domain
    resolves nothing, exactly like a local part no row answers to.

    A local part answers to at most one row — `uq_property_intake_address_local_part`
    is where that is enforced — so there is nothing to choose between: the row
    is active or it is revoked, and the caller branches on the flag. If that
    unique were ever dropped, `one_or_none` raises rather than silently
    picking one of them.

    The recipient is unwrapped exactly as `intake._domain_of` unwraps the
    SENDER, and for the same reason: a worker forwarding `message.to` verbatim
    can hand over `<na-abc@intake.example.test>`. Left bracketed, the `>` would
    ride on the domain and fail the compare below, and the `<` would fail
    `intake.local_part`'s alphabet — so the message would answer 200
    `unknown_address`, the worker would NOT forward it to the fallback mailbox,
    and the night's report would be gone with no event to show for it.
    `test_a_bracketed_recipient_resolves_like_a_bare_one` is the pin.
    """
    envelope_to = envelope_to.strip().strip("<>").strip()
    if envelope_to.rpartition("@")[2].strip().lower() != domain.strip().lower():
        return None
    try:
        local = local_part(envelope_to)
    except ValueError:
        return None
    with factory() as session:
        row = session.scalars(
            select(PropertyIntakeAddress)
            .where(PropertyIntakeAddress.local_part == local)
        ).one_or_none()
        if row is None:
            return None
        return (row.address_id, row.org_id, row.property_id, row.sender_domains,
                row.revoked_at is not None)


async def _read_body(request: Request, max_bytes: int) -> bytes:
    """The raw message, refused at `email_intake_max_bytes`.

    Streamed and capped rather than read whole: the body is an unredacted
    night-audit report and the cap is what stops an unbounded one being held in
    memory at all. 413 is deliberate — it is non-2xx, so the worker forwards
    the message to the fallback mailbox instead of dropping it (D-OH23.9).
    """
    buffer = bytearray()
    async for chunk in request.stream():
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(status_code=413, detail="message too large")
    return bytes(buffer)


@router.post("/email")  # UNGATED: the HMAC below is the whole authentication.
async def receive_email(request: Request) -> dict[str, object]:
    settings = get_settings()
    raw = await _read_body(request, settings.email_intake_max_bytes)

    # One refusal for every authentication failure, with no detail: which
    # check failed is itself a signal, and the caller is unauthenticated until
    # this returns.
    if not verify_signature(
        settings.email_intake_secret,
        request.headers.get("X-Intake-Timestamp", ""),
        raw,
        request.headers.get("X-Intake-Signature", ""),
        now=datetime.now(UTC),
        window=settings.email_intake_window_seconds,
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    limiter: RateLimiter = request.app.state.intake_rate_limiter
    if not limiter.allow(_RATE_LIMIT_KEY):
        raise HTTPException(
            status_code=429, detail="too many messages; try again shortly",
            headers={"Retry-After": "60"},
        )

    envelope_from = request.headers.get("X-Intake-From", "")
    auth_result = request.headers.get("X-Intake-Auth", "")

    base: SessionFactory = request.app.state.db_session_factory
    found = _resolve_address(
        base, request.headers.get("X-Intake-To", ""), settings.email_intake_domain
    )
    if found is None:
        # No org, so nothing is recorded (D-OH23.7). The worker logs it.
        # Returning here is also what keeps a message for a local part we
        # never minted from being opened at all: the MIME parse is below.
        return {"outcome": UNKNOWN_ADDRESS, "attachments": []}
    address_id, org_id, property_id, sender_domains, revoked = found

    # The message is parsed twice, once for these headers and once inside
    # `attachments_of`; two cheap passes are worth more than threading a
    # parsed message through the pure-function boundary `usali.intake` keeps.
    subject, message_id = _subject_and_message_id(raw)

    _inbox, processed_dir, failed_dir = request.app.state.ingest_dirs
    factory = OrgBoundSessionFactory(base, org_id)
    with factory() as session:
        def _finish(outcome: IntakeOutcome,
                    entries: Sequence[dict[str, object]] = ()) -> dict[str, object]:
            _record(
                session, address_id=address_id, property_id=property_id,
                envelope_from=envelope_from, auth_result=auth_result,
                subject=subject, message_id=message_id, outcome=outcome,
                entries=entries,
            )
            return {"outcome": outcome, "attachments": list(entries)}

        if revoked:
            # Recorded, not silently dropped: the operator's evidence that the
            # PMS is still sending to a rotated address (D-OH23.7).
            return _finish("revoked_address")
        if not sender_allowed(auth_result or None, envelope_from, sender_domains):
            return _finish("sender_rejected")

        prop = session.get(Property, property_id)
        if prop is None:
            # fk_property_intake_address_property_org makes the address's
            # (org_id, property_id) a live property row, so this is a database
            # the FK no longer holds in. 5xx: the worker forwards the message
            # to the fallback mailbox rather than this route inventing a
            # disposition for it.
            raise HTTPException(
                status_code=500,
                detail=f"intake address names no property in org {org_id}",
            )

        entries = _attachment_entries(
            session, raw, property_id=property_id, pms_source=prop.pms_source,
            processed_dir=processed_dir, failed_dir=failed_dir,
        )
        if not entries:
            return _finish("no_attachment")
        return _finish(_aggregate(entries), entries)
