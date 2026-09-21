"""Emailed night-audit intake (OH-23): the pieces shared by the webhook, the
mail worker's contract, and the night-audit upload endpoint.

Nothing here touches the database except `validate_for_property` and its two
halves, which read the property detection registry. Nothing here writes a file
or stages a row: this module decides *whether* a message and its attachments
may proceed, and the callers own what happens next.

The validator (`validate_single`, `validate_pack`, `validate_for_property`) is
the code `night_audit_api.upload_night_audit_report` and
`night_audit_api._ingest_pack` run for their property and report-type checks,
so an emailed report is refused for the same reasons a hand-uploaded one is.
The BUSINESS-DATE check is deliberately not here: the upload endpoint keeps it
(D-OH23.5 says an emailed backfill is the point), and it is the reason the
endpoint asks for `sections` back rather than only an outcome.
"""

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes, policy

from sqlalchemy.orm import Session

from usali.adaptors.pack import ReportSection, split_pack
from usali.adaptors.pdf import Word, extract_pages_from_bytes
from usali.adaptors.reader import is_pdf, is_xlsx, read_words_from_bytes
from usali.detect import Detection, detect, load_registry
from usali.night_audit import REQUIRED_REPORTS

# D-OH23.7's closed outcome set, duplicated from the CHECK on
# email_intake_event. The two copies are held together by
# tests/test_intake_schema.py::test_intake_outcomes_match_the_check_constraint.
INTAKE_OUTCOMES: frozenset[str] = frozenset({
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
})

# The subset `validate_for_property` can answer with. Kept as its own name so a
# caller can branch on "the validator refused" without re-listing the strings,
# and so the containment is assertable (tests/test_intake.py::
# test_every_validator_outcome_is_one_the_event_table_accepts).
VALIDATION_OUTCOMES: frozenset[str] = frozenset({
    "wrong_property", "not_a_night_audit_report", "unreadable",
})

# An intake address's local part: `na-` plus base32 of 16 random bytes,
# lowercased and unpadded (26 characters, 128 bits). The DB spells the same
# lowercase rule as ck_property_intake_address_local_part_lower.
_LOCAL_PART_ALPHABET = re.compile(r"[a-z0-9-]+\Z")
_LOCAL_PART_RANDOM_BYTES = 16

_SIGNATURE_PREFIX = "sha256="
_SIGNATURE_HEX_LEN = 64

# Authentication-Results comments and quoted strings, removed before the header
# is tokenized so that `dkim=pass` written inside one is not read as a result.
_AUTH_COMMENT = re.compile(r"\([^()]*\)")
_AUTH_QUOTED = re.compile(r'"[^"]*"')
_AUTH_SEPARATORS = re.compile(r"[;\s]+")


def _safe_component(value: str) -> str:
    """Reduce request-controlled text to something safe to splice into a path."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


# --- webhook signature (D-OH23.2) --------------------------------------------


def verify_signature(
    secret: str, timestamp: str, body: bytes, header: str, *,
    now: datetime, window: int,
) -> bool:
    """Is `header` a valid signature over `timestamp + "\\n" + body`?

    The sender computes HMAC-SHA256 with `secret` and sends the digest as
    `sha256=` + lowercase hex. A timestamp further than `window` seconds from
    `now` in EITHER direction is refused, so a captured call cannot be replayed
    later and a forward-dated one cannot buy itself a longer life.

    Every malformed input — a non-numeric timestamp, a missing or wrong prefix,
    non-hex or wrong-length digest — answers False. It never raises: the caller
    is an unauthenticated route, and a traceback there is a different response
    than a refusal, which is itself a signal.
    """
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(now.timestamp() - sent_at) > window:
        return False
    if not header.startswith(_SIGNATURE_PREFIX):
        return False
    hexdigest = header[len(_SIGNATURE_PREFIX):]
    if len(hexdigest) != _SIGNATURE_HEX_LEN:
        return False
    try:
        sent = bytes.fromhex(hexdigest)
    except ValueError:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("utf-8") + b"\n" + body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(expected, sent)


# --- addresses (D-OH23.3) ----------------------------------------------------


def local_part(envelope_to: str) -> str:
    """The local part of an intake address, lowercased and checked.

    The local part is looked up as a row key, so the alphabet is narrowed to
    what `new_local_part` can mint: `[a-z0-9-]`. A `+tag`, a dot, a space or a
    path separator is refused rather than normalized away — an address that is
    not one we minted has no row to find, and quietly rewriting it would search
    for a DIFFERENT address than the one the message was sent to.
    """
    local, at, _domain = envelope_to.rpartition("@")
    if not at:
        raise ValueError(f"not an email address: {envelope_to!r}")
    local = local.strip().lower()
    if not _LOCAL_PART_ALPHABET.fullmatch(local):
        raise ValueError(f"not an intake local part: {envelope_to!r}")
    return local


def new_local_part() -> str:
    """Mint an unguessable local part: `na-` + 26 lowercase base32 characters."""
    raw = base64.b32encode(secrets.token_bytes(_LOCAL_PART_RANDOM_BYTES))
    return "na-" + raw.decode("ascii").rstrip("=").lower()


# --- sender policy (D-OH23.4) ------------------------------------------------


def _auth_tokens(auth_result: str) -> set[str]:
    stripped = _AUTH_QUOTED.sub(" ", auth_result)
    # One pass is enough for the flat comments a real Authentication-Results
    # carries; a nested comment leaves its outer text behind, which can only
    # cost a pass, never grant one, because what remains is compared for
    # equality against `dkim=pass` / `spf=pass`.
    stripped = _AUTH_COMMENT.sub(" ", stripped)
    return {t.lower() for t in _AUTH_SEPARATORS.split(stripped) if t}


def sender_allowed(
    auth_result: str | None, envelope_from: str, sender_domains: Sequence[str] | None,
) -> bool:
    """Two layers, both of which must hold.

    First, the message must be authenticated: the `Authentication-Results` text
    must carry `dkim=pass` or `spf=pass` as its own TOKEN. A substring test
    would accept `dkim=passthrough` and anything a relay wrote into a comment,
    so quoted strings and parenthesised comments are removed before splitting.

    Second, if the address carries a non-empty `sender_domains`, the envelope
    sender's domain must be in it. An EMPTY list means no domains were
    configured and reads exactly like `None`; it is not "permit nothing".
    """
    if not auth_result:
        return False
    if not ({"dkim=pass", "spf=pass"} & _auth_tokens(auth_result)):
        return False
    if not sender_domains:
        return True
    domain = envelope_from.rpartition("@")[2].strip().lower()
    return domain in {d.strip().lower() for d in sender_domains}


# --- attachments -------------------------------------------------------------


def attachments_of(raw: bytes) -> list[tuple[str, bytes]]:
    """The PDF and XLSX parts of a MIME message, as (scrubbed name, bytes).

    Parts are kept by MAGIC BYTES, never by declared content type or file
    suffix — the same is_pdf/is_xlsx pair the reader dispatches on and the
    upload endpoint gates on. A part that is neither is dropped however it is
    labelled, so a text body, a signature image and a `.pdf` that is really an
    executable all fall out here.

    Names are reduced by `_safe_component`; a part with no filename is named
    `attachment-N` with the extension its magic bytes imply. Unparseable bytes
    yield an empty list rather than an exception: the caller records an
    outcome for a message it could not open, and has nothing to re-raise into.
    """
    try:
        message = message_from_bytes(raw, policy=policy.default)
    except Exception:
        return []
    found: list[tuple[str, bytes]] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if not isinstance(payload, bytes):
            continue
        if is_pdf(payload):
            extension = ".pdf"
        elif is_xlsx(payload):
            extension = ".xlsx"
        else:
            continue
        filename = part.get_filename()
        name = filename if filename else f"attachment-{len(found) + 1}{extension}"
        found.append((_safe_component(name), payload))
    return found


# --- the shared night-audit validator (D-OH23.5) -----------------------------


@dataclass(frozen=True)
class ValidatedSection:
    """One recognized report. `title` is the pack section's title row, or None
    for a single report, which has no section title to speak of."""

    title: str | None
    words: list[Word]
    detection: Detection


@dataclass(frozen=True)
class Validation:
    """`outcome` is None when the document passed. `detail` is the refusal text
    the upload endpoint raises as its 422; it is empty on a pass.

    `sections` and `skipped_titles` are empty on a refusal — a caller that gets
    an outcome has nothing further to do with the document."""

    outcome: str | None
    detail: str
    sections: tuple[ValidatedSection, ...] = ()
    skipped_titles: tuple[str, ...] = ()


def _required_reports(pms_source: str) -> set[str]:
    return {report_type for report_type, _label in
            REQUIRED_REPORTS.get(pms_source.upper(), ())}


def validate_single(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> Validation:
    """The single-report checks: readable, this property, a required report."""
    try:
        words = read_words_from_bytes(data)
        det = detect(words, load_registry(session))
    except Exception as exc:
        return Validation("unreadable", f"could not read report: {exc}")
    if det.property_id != property_id:
        return Validation(
            "wrong_property",
            f"report is for property {det.property_id}, not {property_id}",
        )
    required = _required_reports(pms_source)
    if det.report_type not in required:
        return Validation(
            "not_a_night_audit_report",
            f"{det.report_type} is not part of this property's night audit "
            f"({pms_source} requires: {', '.join(sorted(required))})",
        )
    return Validation(None, "", (ValidatedSection(None, words, det),))


def validate_sections(
    session: Session, sections: Sequence[ReportSection], property_id: str, pms_source: str
) -> Validation:
    """The same two checks, per RECOGNIZED section of an already-split pack.

    A section is recognized by its TITLE, which is the call
    `ingestion.process_pack_bytes` makes over these same sections; a section
    whose title matches nothing, or whose property does not resolve, is skipped
    by title exactly as ingestion skips it. The first failing section refuses
    the whole pack, so nothing from a pack carrying one wrong-property section
    reaches the ingest path.

    Takes sections rather than bytes so a caller that has already split the
    pack — `night_audit_api._ingest_pack`, which owns its own 422 for a pack it
    cannot read — runs these checks without splitting twice.
    """
    registry = load_registry(session)
    required = _required_reports(pms_source)
    recognized: list[ValidatedSection] = []
    skipped_titles: list[str] = []
    for section in sections:
        try:
            # The section TITLE decides the report signature — the same call
            # ingestion.process_pack_bytes makes over these same sections. A
            # `Rate Plan` COLUMN heading inside the 120-word header window
            # matches an earlier signature (issue #78), which skipped the
            # section here while ingestion recognized it. Pinned by
            # tests/test_night_audit.py::
            # test_pack_validation_recognizes_a_section_by_its_title.
            det = detect(section.words, registry, section.title)
        except ValueError:
            skipped_titles.append(section.title or "(untitled)")
            continue
        recognized.append(ValidatedSection(section.title, section.words, det))
        if det.property_id != property_id:
            return Validation(
                "wrong_property",
                f"pack section {section.title!r} is for property "
                f"{det.property_id}, not {property_id}",
            )
        if det.report_type not in required:
            return Validation(
                "not_a_night_audit_report",
                f"pack section {section.title!r}: {det.report_type} is not part of "
                f"this property's night audit ({pms_source} requires: "
                f"{', '.join(sorted(required))})",
            )
    if not recognized:
        return Validation(
            "not_a_night_audit_report", "no recognized report sections in this pack"
        )
    return Validation(None, "", tuple(recognized), tuple(skipped_titles))


def validate_pack(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> Validation:
    """Split a pack held in memory, then run `validate_sections` over it."""
    try:
        sections = split_pack(extract_pages_from_bytes(data))
    except Exception as exc:
        return Validation("unreadable", f"could not read the pack: {exc}")
    return validate_sections(session, sections, property_id, pms_source)


def validate_document(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> Validation:
    """Validate a document whose shape is not known in advance.

    The routing MIRRORS `ingestion.process_document_bytes`: single-report
    detection is probed first and only a ValueError from `detect` selects the
    pack path, so the validator's answer and the ingest path's answer are
    reached the same way. Routing by section count instead would split a
    single multi-page report into sections and drop pages (issue #138).
    Reading the bytes is not a routing signal either: a PDF that will not parse
    takes the single-report path and comes back `unreadable`.
    """
    if is_pdf(data):
        try:
            words = read_words_from_bytes(data)
        except Exception:
            words = None
        if words is not None:
            try:
                detect(words, load_registry(session))
            except ValueError:
                return validate_pack(session, data, property_id, pms_source)
    return validate_single(session, data, property_id, pms_source)


def validate_for_property(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> str | None:
    """None when the document is a night-audit report for this property, else
    one of VALIDATION_OUTCOMES."""
    return validate_document(session, data, property_id, pms_source).outcome
