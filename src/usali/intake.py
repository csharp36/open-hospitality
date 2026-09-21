"""Emailed night-audit intake (OH-23): the message-level primitives.

Webhook signature verification, intake address local parts, the sender policy,
and pulling the report attachments out of a MIME message. Everything here is a
pure function of its arguments, and the import graph is deliberately shallow —
no database driver, no filesystem, and no PDF or XLSX parser; format detection
comes from the leaf module `usali.adaptors.magic`. That is what lets the
unauthenticated webhook import this module and decide whether to go any
further before it opens anything, and it is pinned by
tests/test_intake.py::test_intake_imports_no_parser.

The night-audit checks a message's attachments must then pass live in
`usali.night_audit_validation`, which does read the database.

WHAT THE SENDER POLICY RESTS ON. `sender_allowed` reads a decision out of a
free-text header that a RECEIVER formats, and receivers interpolate
attacker-influenced text into it. `_auth_results` and the dot-atom rule in
`sender_allowed` are written against that, but they bound the damage rather
than remove the dependency: a receiver that echoed an attacker-chosen string
containing `;` into its comment could still manufacture a result. The design
(D-OH23.4) records the assumption this rests on — that Cloudflare's
Authentication-Results echoes the MAIL FROM address and the connecting IP, not
the HELO string — as an assumption, not something measured here. If
`X-Intake-Auth` is ever sourced from a different receiver, the free-text
policy has to be replaced by verifying DKIM over the raw message in-process.
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

from usali.adaptors.magic import is_pdf, is_xlsx

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
# Unix seconds, bounded. `int()` itself is happy to build an arbitrarily large
# integer, and converting one to a float for the window comparison raises
# OverflowError — on an unauthenticated route, a raised exception is a 500 and
# a 500 is an oracle. 12 digits reaches the year 33658.
_TIMESTAMP = re.compile(r"\d{1,12}\Z")

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

# RFC 5322 atext, and a dot-atom built from it. An envelope local part outside
# this alphabet is refused (`sender_allowed`): every character a receiver's
# free-text comment could be broken out of — `(`, `)`, `;`, space, `"` — is
# outside atext, so a MAIL FROM that could be echoed as a forged result cannot
# reach the parser in the first place. Quoted local parts are legal in RFC 5322
# and are refused here anyway; a PMS night-audit mailer does not use one.
_ATEXT = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]"
_DOT_ATOM = re.compile(rf"{_ATEXT}+(?:\.{_ATEXT}+)*\Z")
# Deliberately narrow: letters, digits, dot and hyphen. Applied to the envelope
# sender's domain and to every domain read out of the header, so a domain that
# could carry a separator never reaches an alignment comparison.
_DOMAIN = re.compile(r"[A-Za-z0-9.-]+\Z")


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

    Every malformed input — a non-numeric or absurdly long timestamp, a
    missing or wrong prefix, non-hex or wrong-length digest — answers False.
    It never raises: the caller is an unauthenticated route, and a traceback
    there is a different response than a refusal, which is itself a signal.
    Pinned over random input by tests/test_intake.py::
    test_no_timestamp_makes_verify_signature_raise.
    """
    if not _TIMESTAMP.fullmatch(timestamp):
        return False
    sent_at = int(timestamp)
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

    Three rules, each of which answers NO RESULTS AT ALL rather than guess:

    1. Quoted strings go first, then comments are stripped to a FIXPOINT.
       `_AUTH_COMMENT` matches innermost parentheses only, so one pass over
       `((x) dkim=pass)` leaves `( dkim=pass)`, whose surviving text tokenizes
       into a live pass. If a parenthesis is still standing after
       `_AUTH_COMMENT_ROUNDS` rounds the header is unbalanced and it is
       refused whole.
    2. Results are cut at `;` — RFC 8601 puts one result per resinfo — and a
       `method=` token may only appear as the FIRST `key=value` token of its
       clause. A method token met anywhere else is a result that arrived
       without a separator of its own, which is what a comment BREAKOUT
       produces, and it refuses the header.

    Rule 2 is the one that matters. Receivers interpolate the MAIL FROM into
    the SPF comment, so a sender whose local part is `a) dkim=pass
    header.d=victim.test (b` closes the receiver's comment, writes its own
    result, and reopens a comment that the closing paren balances — the
    header ends up well formed, with one real result and one forged one. The
    forged result has no `;` in front of it, and that is what is detectable.
    The other half of the defense is in `sender_allowed`, which refuses an
    envelope local part that is not a dot-atom, so such an address cannot be
    accepted whatever the receiver does with it.

    Pinned by tests/test_intake.py::test_a_comment_breakout_grants_no_pass,
    ::test_a_result_without_its_own_separator_refuses_the_header,
    ::test_a_nested_comment_grants_no_pass and
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
        seen_key_value = False
        for token in clause.split():
            key, separator, value = token.partition("=")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip().lower()
            if key in _AUTH_METHODS:
                if seen_key_value:
                    return []  # a result that brought no `;` of its own
                current = _AuthResult(method=key, verdict=value, properties={})
                results.append(current)
            elif "." in key and current is not None:
                current.properties[key] = value
            seen_key_value = True
    return results


def _domain_of(address: str) -> str:
    """The domain of an envelope address, lowercased, or "" if the address is
    not one this policy will act on.

    Empty when there is no domain at all — a bare local part or a null
    return-path (`<>`) aligns with nothing. Empty ALSO when the local part is
    not a dot-atom or the domain is not `[A-Za-z0-9.-]+`: every character a
    receiver's comment could be broken out of is outside those alphabets, so
    an address that could be echoed into the header as a forged result never
    gets as far as being compared against one. A quoted local part
    (`"a b"@hotel.test`) is legal mail and is refused here; a PMS night-audit
    mailer does not send from one.

    Angle brackets and surrounding space are trimmed, because a worker that
    forwards `message.from` verbatim can hand over `<gm@hotel.test>`, and
    leaving the `>` on the domain would refuse every such sender.
    """
    local, at, domain = address.strip().strip("<>").strip().rpartition("@")
    if not at or not local:
        return ""
    local, domain = local.lower(), domain.strip().lower()
    if not _DOT_ATOM.fullmatch(local) or not _DOMAIN.fullmatch(domain):
        return ""
    return domain


def _signing_domain(value: str) -> str:
    """A `header.d` property, or "" if it is not a domain.

    A value outside `_DOMAIN` is not a domain any DKIM signer could have used,
    so it names nothing and the result it belongs to can vouch for nothing.
    """
    value = value.strip().lower()
    return value if _DOMAIN.fullmatch(value) else ""


def _mailfrom_domain(value: str) -> str:
    """The domain out of an `smtp.mailfrom` property, or "" if the value is not
    one of the three shapes a receiver writes it in: a bare domain, a whole
    address, or `@domain`. Anything else — two `@`s, a local part outside
    atext, a domain carrying a separator — is not read leniently; the property
    is treated as absent, and an absent property vouches for nobody.
    """
    value = value.strip().lower()
    local, at, domain = value.rpartition("@")
    if at and local and not _DOT_ATOM.fullmatch(local):
        return ""
    return domain if _DOMAIN.fullmatch(domain) else ""


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
      `_mailfrom_domain` reduces those three and refuses everything else.
    * A pass carrying no usable property proves nothing and is a fail.
    """
    for result in _auth_results(auth_result):
        if result.verdict != "pass":
            continue
        if result.method == "dkim":
            if _aligned(_signing_domain(result.properties.get("header.d", "")),
                        envelope_domain):
                return True
        elif result.method == "spf":
            mailfrom = _mailfrom_domain(result.properties.get("smtp.mailfrom", ""))
            if mailfrom and mailfrom == envelope_domain:
                return True
    return False


def sender_allowed(
    auth_result: str | None, envelope_from: str, sender_domains: Sequence[str] | None,
) -> bool:
    """Two layers, both of which must hold.

    Before either, the envelope sender must be an address this policy will
    act on at all: a dot-atom local part at a `[A-Za-z0-9.-]` domain
    (`_domain_of`). That is not cosmetic — a local part carrying `(`, `)`,
    `;`, a space or a quote is exactly what a receiver echoes into its
    Authentication-Results comment to forge a result, and refusing it here
    means no such address is ever the subject of a comparison.

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


def _attachment_name(
    filename: str | None, extension: str, ordinal: int, taken: set[str]
) -> str:
    """The name one attachment is given: a scrubbed, bounded stem plus the
    FINAL extension its magic bytes imply.

    The declared final suffix is dropped and the magic one put in its place —
    `lies.pdf` carrying XLSX bytes comes back `lies.xlsx` — because the
    extension is what a later reader and a human both read as the format
    claim. Only the final suffix moves: `report.xlsx` holding a PDF becomes
    `report.pdf`, while `report.2026.xlsx` keeps the `.2026`. A stem that
    scrubs away to nothing, `.`, or `..` falls back to `attachment-N`.

    `taken` holds the names already used for THIS message; a repeat gets its
    ordinal appended, so two parts both called `same.pdf` come back as
    `same.pdf` and `same-2.pdf` rather than one name for two files.
    """
    scrubbed = safe_component(filename or "")
    head, dot, _tail = scrubbed.rpartition(".")
    stem = head if dot and head else scrubbed
    stem = stem[: _ATTACHMENT_NAME_MAX - len(extension)]
    if stem in _UNUSABLE_STEMS:
        return f"attachment-{ordinal}{extension}"
    name = f"{stem}{extension}"
    if name not in taken:
        return name
    suffix = f"-{ordinal}"
    stem = stem[: _ATTACHMENT_NAME_MAX - len(extension) - len(suffix)]
    return f"{stem}{suffix}{extension}"


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
    `..`, is at most 120 characters, is distinct within one message, and ends
    in the extension the part's magic bytes imply (`_attachment_name`).
    Unparseable bytes yield an empty
    list rather than an exception: the caller records an outcome for a message
    it could not open, and has nothing to re-raise into.
    """
    try:
        message = message_from_bytes(raw, policy=policy.default)
    except Exception:
        return []
    found: list[tuple[str, bytes]] = []
    taken: set[str] = set()
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
        name = _attachment_name(
            part.get_filename(), extension, len(found) + 1, taken
        )
        taken.add(name)
        found.append((name, payload))
    return found
