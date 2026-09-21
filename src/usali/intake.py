"""Emailed night-audit intake (OH-23): the pieces shared by the webhook, the
mail worker's contract, and the night-audit upload endpoint.

Nothing here touches the database except `validate_for_property` and its two
halves, which read the property detection registry. Nothing here writes a file
or stages a row: this module decides *whether* a message and its attachments
may proceed, and the callers own what happens next.

The validator (`validate_single`, `validate_sections`, `validate_pack`,
`validate_for_property`) is the code `night_audit_api.upload_night_audit_report`
and `night_audit_api._ingest_pack` call for their property and report-type
checks, so a second caller can run the same two checks over the same bytes.
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
from typing import Literal, get_args

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

# The subset the validator can answer with, closed IN THE TYPE: mypy --strict
# refuses a fourth string at every `Validation(...)` site, so the set cannot
# drift by someone inventing an outcome the event table has no room for. The
# frozenset is derived from the Literal rather than written twice, and its
# containment in INTAKE_OUTCOMES is asserted by tests/test_intake.py::
# test_every_validator_outcome_is_one_the_event_table_accepts.
ValidationOutcome = Literal["wrong_property", "not_a_night_audit_report", "unreadable"]
VALIDATION_OUTCOMES: frozenset[str] = frozenset(get_args(ValidationOutcome))

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

# Method names that START a result inside an Authentication-Results clause.
# A token whose key is one of these opens a new result; a token whose key
# carries a dot (`header.d`, `smtp.mailfrom`) is one of that result's
# properties; anything else (`reason=`, `policy=`) is ignored.
_AUTH_METHODS = frozenset({
    "dkim", "spf", "dmarc", "iprev", "auth", "arc", "dkim-adsp", "sender-id",
})


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


@dataclass
class _AuthResult:
    method: str
    verdict: str
    properties: dict[str, str]


def _auth_results(auth_result: str) -> list[_AuthResult]:
    """Parse Authentication-Results into (method, verdict, properties) triples.

    Quoted strings and parenthesised comments are removed first, so a
    `dkim=pass` written inside one is not read as a result. One comment pass is
    enough for the flat comments a real header carries; a nested comment leaves
    its outer text behind, which can only cost a pass, never grant one, because
    what survives is compared for EQUALITY against a method name.

    Results are cut at `;` (RFC 8601 puts one result per resinfo), so a
    property never attaches across a boundary to the wrong method.
    """
    text = _AUTH_COMMENT.sub(" ", _AUTH_QUOTED.sub(" ", auth_result))
    results: list[_AuthResult] = []
    for clause in text.split(";"):
        current: _AuthResult | None = None
        for token in clause.split():
            key, separator, value = token.partition("=")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip().lower()
            if key in _AUTH_METHODS:
                current = _AuthResult(method=key, verdict=value, properties={})
                results.append(current)
            elif "." in key and current is not None:
                current.properties[key] = value
    return results


def _domain_of(address: str) -> str:
    """The domain of an envelope address, lowercased. Empty when there is none
    — a bare local part or a null return-path aligns with nothing."""
    local, at, domain = address.strip().rpartition("@")
    if not at or not local:
        return ""
    return domain.strip().lower()


def _aligned(signing_domain: str, envelope_domain: str) -> bool:
    """DMARC RELAXED alignment: the signing domain must be the envelope
    sender's domain, or a parent of it ON A LABEL BOUNDARY — `d=vendor.com`
    vouches for `x@mail.vendor.com`, but `d=nothotel.test` does not vouch for
    `hotel.test` and `d=hotel.test` does not vouch for `nothotel.test`. The
    leading dot in the suffix test is what makes that boundary; a bare
    `endswith` is the bug this spells out.
    """
    if not signing_domain or not envelope_domain:
        return False
    return (
        signing_domain == envelope_domain
        or envelope_domain.endswith(f".{signing_domain}")
    )


def _authenticated_for(auth_result: str, envelope_domain: str) -> bool:
    """Is there a dkim or spf PASS that vouches for `envelope_domain`?

    A pass on its own proves only that SOMEBODY's mail was authenticated: a
    sender who holds a valid DKIM key for their own domain gets
    `dkim=pass header.d=theirs` on a message whose envelope sender is anyone
    at all. D-OH23.4 requires the pass be FOR the envelope sender's domain, so:

    * `dkim=pass` counts when its `header.d` aligns (relaxed) with the
      envelope domain. Several dkim results may appear; one aligned pass is
      enough, which is what a forwarder adding its own signature produces.
    * `spf=pass` counts when its `smtp.mailfrom` domain EQUALS the envelope
      domain. SPF authenticates the MAIL FROM itself, so there is no parent to
      relax to, and `smtp.helo` is not the MAIL FROM and does not count.
    * A pass carrying no usable property proves nothing and is a fail.
    """
    for result in _auth_results(auth_result):
        if result.verdict != "pass":
            continue
        if result.method == "dkim":
            if _aligned(result.properties.get("header.d", ""), envelope_domain):
                return True
        elif result.method == "spf":
            mailfrom = result.properties.get("smtp.mailfrom", "")
            # Some receivers print the whole MAIL FROM address here.
            if _domain_of(mailfrom) == envelope_domain or mailfrom == envelope_domain:
                return True
    return False


def sender_allowed(
    auth_result: str | None, envelope_from: str, sender_domains: Sequence[str] | None,
) -> bool:
    """Two layers, both of which must hold.

    First, the message must be authenticated FOR THE ENVELOPE SENDER'S DOMAIN:
    a `dkim=pass` or `spf=pass` that vouches for somebody else buys nothing
    (`_authenticated_for` is where that binding is made).

    Second, if the address carries a non-empty `sender_domains`, the envelope
    sender's domain must be in it EXACTLY — the operator lists the sending
    domain after reading it off the event log, so there is nothing to relax.
    An EMPTY list means no domains were configured and reads exactly like
    `None`; it is not "permit nothing".
    """
    if not auth_result:
        return False
    domain = _domain_of(envelope_from)
    if not domain:
        return False
    if not _authenticated_for(auth_result, domain):
        return False
    if not sender_domains:
        return True
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

    outcome: ValidationOutcome | None
    detail: str
    sections: tuple[ValidatedSection, ...] = ()
    skipped_titles: tuple[str, ...] = ()
    # The exception behind an `unreadable`, so a caller raising an HTTP error
    # can chain it (`raise ... from checked.cause`) instead of losing the
    # traceback the old inline `except` block kept. None for every outcome
    # that is a judgement rather than a failure.
    cause: BaseException | None = None


def _required_reports(pms_source: str) -> set[str]:
    return {report_type for report_type, _label in
            REQUIRED_REPORTS.get(pms_source.upper(), ())}


def validate_single(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> Validation:
    """The single-report checks: readable, this property, a required report.

    The two failures the old inline block treated alike are separated: bytes
    the READER cannot turn into words are `unreadable`, while words that no
    report signature claims, or whose property is not registered, are a
    detection failure and answer `not_a_night_audit_report`. The 422 text is
    the same either way, which is what keeps the upload endpoint's wording
    unchanged.
    """
    try:
        words = read_words_from_bytes(data)
    except Exception as exc:
        return Validation("unreadable", f"could not read report: {exc}", cause=exc)
    try:
        det = detect(words, load_registry(session))
    except ValueError as exc:
        return Validation(
            "not_a_night_audit_report", f"could not read report: {exc}", cause=exc
        )
    except Exception as exc:
        return Validation("unreadable", f"could not read report: {exc}", cause=exc)
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
    `ingestion.process_pack_bytes` makes over these same sections. A section
    this function cannot detect — no signature matches its title, or no
    registered property resolves — is dropped here, and only its title is
    kept. The first failing section refuses the whole pack, so nothing from a
    pack carrying one wrong-property section reaches the ingest path.

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
        return Validation("unreadable", f"could not read the pack: {exc}", cause=exc)
    return validate_sections(session, sections, property_id, pms_source)


def validate_document(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> Validation:
    """Validate a document whose shape is not known in advance.

    Single-report detection is probed first, and only a ValueError from
    `detect` selects the pack path. Routing by section count instead would
    split a single multi-page report into sections and drop pages (issue
    #138). Reading the bytes is not a routing signal either: a PDF that will
    not parse takes the single-report path and comes back `unreadable`.

    This rule is written to match the one `ingestion.process_document_bytes`
    routes on, so the validator and the ingest that follows it do not disagree
    about a document's shape. That agreement is pinned by
    tests/test_intake.py::test_routing_agrees_with_the_ingest_path, over the
    two committed samples that go opposite ways.
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
) -> ValidationOutcome | None:
    """None when the document is a night-audit report for this property, else
    one of VALIDATION_OUTCOMES."""
    return validate_document(session, data, property_id, pms_source).outcome
