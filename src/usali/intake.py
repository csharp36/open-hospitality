"""Emailed night-audit intake (OH-23): the message-level primitives.

Webhook signature verification, intake address local parts, the sender policy,
and pulling the report attachments out of a MIME message. Everything here is a
pure function of its arguments: no database, no filesystem, and no PDF or XLSX
adaptor, which is what lets the unauthenticated webhook import this module and
decide whether to go any further before it opens anything.

The night-audit checks a message's attachments must then pass live in
`usali.night_audit_validation`, which does read the database.
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

from usali.adaptors.reader import is_pdf, is_xlsx

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

# An intake address's local part: `na-` plus base32 of 16 random bytes,
# lowercased and unpadded (26 characters, 128 bits). The DB spells the same
# lowercase rule as ck_property_intake_address_local_part_lower.
_LOCAL_PART_ALPHABET = re.compile(r"[a-z0-9-]+\Z")
_LOCAL_PART_RANDOM_BYTES = 16

_SIGNATURE_PREFIX = "sha256="
_SIGNATURE_HEX_LEN = 64

# An attachment name is spliced into the `source_file` an ingest records and
# into the filed artifact's name, so it is bounded as well as scrubbed. 120 is
# comfortably under every filesystem's per-component limit with room for the
# `night-audit-<property_id>-` prefix night_audit_api adds.
_ATTACHMENT_NAME_MAX = 120
# What a scrubbed stem may not be, mirroring the upload endpoint's own refusal
# of `.` and `..` as a filename (night_audit_api.upload_night_audit_report,
# pinned by tests/test_night_audit.py::test_upload_refuses_unsafe_filenames).
_UNUSABLE_STEMS = frozenset({"", ".", ".."})

# Authentication-Results comments and quoted strings, removed before the header
# is tokenized so that `dkim=pass` written inside one is not read as a result.
# The comment pattern matches only INNERMOST parentheses, so nesting is undone
# by repeating it; `_AUTH_COMMENT_ROUNDS` bounds that repetition and any
# parenthesis still standing afterwards makes the whole header unparseable.
_AUTH_COMMENT = re.compile(r"\([^()]*\)")
_AUTH_COMMENT_ROUNDS = 8
_AUTH_QUOTED = re.compile(r'"[^"]*"')

# Method names that START a result inside an Authentication-Results clause.
# A token whose key is one of these opens a new result; a token whose key
# carries a dot (`header.d`, `smtp.mailfrom`) is one of that result's
# properties; anything else (`reason=`, `policy=`) is ignored.
_AUTH_METHODS = frozenset({
    "dkim", "spf", "dmarc", "iprev", "auth", "arc", "dkim-adsp", "sender-id",
})


def safe_component(value: str) -> str:
    """Reduce text to the `[A-Za-z0-9._-]` alphabet, one character for one."""
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
    `dkim=pass` written inside one is not read as a result. Comments are
    stripped to a FIXPOINT: `_AUTH_COMMENT` matches innermost parentheses
    only, so one pass over `((x) dkim=pass)` leaves `( dkim=pass)` — and the
    surviving text tokenizes into a live pass. Repeating the substitution
    until it changes nothing removes nesting; if a parenthesis is still
    standing after `_AUTH_COMMENT_ROUNDS` rounds the header is unbalanced,
    which no honest receiver writes, and this returns NO results rather than
    guess where the comment ended. That matters because receivers interpolate
    the MAIL FROM into the SPF comment, so the local part of a crafted sender
    address lands inside these parentheses.

    Results are cut at `;` (RFC 8601 puts one result per resinfo), so a
    property never attaches across a boundary to the wrong method.

    Both behaviors are pinned by tests/test_intake.py::
    test_a_nested_comment_grants_no_pass and
    ::test_an_unbalanced_comment_yields_no_results.
    """
    text = _AUTH_QUOTED.sub(" ", auth_result)
    for _round in range(_AUTH_COMMENT_ROUNDS):
        text, replaced = _AUTH_COMMENT.subn(" ", text)
        if not replaced:
            break
    if "(" in text or ")" in text:
        return []
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
    — a bare local part or a null return-path (`<>`) aligns with nothing.

    Angle brackets and surrounding space are trimmed, because a worker that
    forwards `message.from` verbatim can hand over `<gm@hotel.test>`, and
    leaving the `>` on the domain would refuse every such sender.
    """
    local, at, domain = address.strip().strip("<>").strip().rpartition("@")
    if not at or not local:
        return ""
    return domain.strip().lower()


def _property_domain(value: str) -> str:
    """The domain out of an Authentication-Results property value, which a
    receiver may write as a bare domain, a whole address, or `@domain`."""
    return value.strip().rpartition("@")[2].strip().lower()


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
      relax to, and `smtp.helo` is not the MAIL FROM and does not count. The
      value may be written as a bare domain, a whole address, or `@domain`;
      `_property_domain` reduces all three.
    * A pass carrying no usable property proves nothing and is a fail.
    """
    for result in _auth_results(auth_result):
        if result.verdict != "pass":
            continue
        if result.method == "dkim":
            if _aligned(result.properties.get("header.d", ""), envelope_domain):
                return True
        elif result.method == "spf":
            mailfrom = _property_domain(result.properties.get("smtp.mailfrom", ""))
            if mailfrom and mailfrom == envelope_domain:
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


def _attachment_name(filename: str | None, extension: str, ordinal: int) -> str:
    """The name one attachment is given: a scrubbed, bounded stem plus the
    extension its MAGIC BYTES imply.

    The declared suffix is discarded, not trusted — `lies.pdf` carrying XLSX
    bytes comes back `lies.xlsx`, because the extension is what a later reader
    and a human both read as the format claim. A stem that scrubs away to
    nothing, `.`, or `..` falls back to `attachment-N`.
    """
    scrubbed = safe_component(filename or "")
    head, dot, _tail = scrubbed.rpartition(".")
    stem = head if dot and head else scrubbed
    stem = stem[: _ATTACHMENT_NAME_MAX - len(extension)]
    if stem in _UNUSABLE_STEMS:
        return f"attachment-{ordinal}{extension}"
    return f"{stem}{extension}"


def attachments_of(raw: bytes) -> list[tuple[str, bytes]]:
    """The PDF and XLSX parts of a MIME message, as (name, bytes).

    Parts are kept by MAGIC BYTES, never by declared content type or file
    suffix — the same is_pdf/is_xlsx pair the reader dispatches on and the
    upload endpoint gates on. A part that is neither is dropped however it is
    labelled, so a text body, a signature image and a `.pdf` that is really an
    executable all fall out here. `walk` descends into `message/rfc822` parts,
    so a report inside a FORWARDED message is found (tests/test_intake.py::
    test_a_forwarded_report_is_found).

    Each name is reduced to the `[A-Za-z0-9._-]` alphabet, is never `.` or
    `..`, is at most 120 characters, and ends in the extension the part's
    magic bytes imply (`_attachment_name`). Unparseable bytes yield an empty
    list rather than an exception: the caller records an outcome for a message
    it could not open, and has nothing to re-raise into.
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
        found.append((
            _attachment_name(part.get_filename(), extension, len(found) + 1),
            payload,
        ))
    return found
