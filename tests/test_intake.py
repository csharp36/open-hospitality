"""OH-23: the message-level intake primitives.

Signature verification, intake address local parts, the sender policy, and
pulling report attachments out of a MIME message. The night-audit checks those
attachments must then pass are in tests/test_night_audit_validation.py.
"""

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest

from usali import intake

# --- signature ---------------------------------------------------------------

# A fixed vector, recomputed by hand once with `hmac`:
#   hmac.new(b"s", b"1700000000" + b"\n" + b"hello", hashlib.sha256).hexdigest()
# It is the wire format the sender must produce, so it is written out here
# rather than derived from the module under test.
_VECTOR_SECRET = "s"
_VECTOR_TIMESTAMP = "1700000000"
_VECTOR_BODY = b"hello"
_VECTOR_HEADER = "sha256=7caaa0c43407622ac33872a98db85fa647961b1a4572722c3bb938e5114b1022"

_VECTOR_NOW = datetime.fromtimestamp(int(_VECTOR_TIMESTAMP), UTC)


def _verify(header: str, *, secret: str = _VECTOR_SECRET, now: datetime = _VECTOR_NOW) -> bool:
    return intake.verify_signature(
        secret, _VECTOR_TIMESTAMP, _VECTOR_BODY, header, now=now, window=300
    )


def test_the_signature_vector_verifies():
    assert _verify(_VECTOR_HEADER) is True


def test_the_vector_is_the_documented_construction():
    """The vector is `timestamp + "\\n" + body` under HMAC-SHA256, not some
    other concatenation that happens to round-trip through this module."""
    expected = hmac.new(
        _VECTOR_SECRET.encode(), _VECTOR_TIMESTAMP.encode() + b"\n" + _VECTOR_BODY,
        hashlib.sha256,
    ).hexdigest()
    assert _VECTOR_HEADER == f"sha256={expected}"


@pytest.mark.parametrize("skew", [301, -301, 86400, -86400])
def test_a_timestamp_outside_the_window_is_refused(skew):
    assert _verify(_VECTOR_HEADER, now=_VECTOR_NOW + timedelta(seconds=skew)) is False


@pytest.mark.parametrize("skew", [0, 299, -299, 300, -300])
def test_a_timestamp_inside_the_window_is_accepted(skew):
    """300 is IN: the guard is `> window`, not `>= window`. Without these two
    the boundary is free to move by a second in either direction."""
    assert _verify(_VECTOR_HEADER, now=_VECTOR_NOW + timedelta(seconds=skew)) is True


def test_the_wrong_secret_is_refused():
    assert _verify(_VECTOR_HEADER, secret="not-s") is False


@pytest.mark.parametrize(
    "header",
    [
        "",
        "7caaa0c43407622ac33872a98db85fa647961b1a4572722c3bb938e5114b1022",  # no prefix
        "sha1=7caaa0c43407622ac33872a98db85fa647961b1a4572722c3bb938e5114b1022",
        "sha256=notyhex" * 8,  # right length, not hex
        "sha256=7caaa0c4",  # hex, wrong length
        "sha256=",
        "sha256",
        "sha256=" + "0" * 63,
    ],
)
def test_a_malformed_header_is_false_not_an_exception(header):
    assert _verify(header) is False


def test_a_non_numeric_timestamp_is_false_not_an_exception():
    assert intake.verify_signature(
        _VECTOR_SECRET, "not-a-number", _VECTOR_BODY, _VECTOR_HEADER,
        now=_VECTOR_NOW, window=300,
    ) is False


# --- local parts -------------------------------------------------------------


def test_local_part_takes_the_part_before_the_domain():
    assert intake.local_part("na-abc@intake.example.test") == "na-abc"


def test_local_part_lowercases():
    assert intake.local_part("NA-ABC@Intake.Example.Test") == "na-abc"


@pytest.mark.parametrize(
    "envelope_to",
    [
        "na-abc+tag@intake.example.test",
        "na_abc@intake.example.test",
        "na.abc@intake.example.test",
        "na abc@intake.example.test",
        "na-abc/../etc@intake.example.test",
        "@intake.example.test",
        "na-abc",  # no domain at all
    ],
)
def test_local_part_refuses_anything_outside_the_allowed_alphabet(envelope_to):
    with pytest.raises(ValueError):
        intake.local_part(envelope_to)


def test_a_new_local_part_has_the_documented_shape():
    assert re.fullmatch(r"na-[a-z2-7]{26}", intake.new_local_part())


def test_new_local_parts_do_not_repeat():
    assert len({intake.new_local_part() for _ in range(200)}) == 200


def test_a_new_local_part_survives_its_own_parser():
    generated = intake.new_local_part()
    assert intake.local_part(f"{generated}@intake.example.test") == generated


# --- sender policy -----------------------------------------------------------
#
# A pass is only worth something if it vouches for THIS envelope sender. The
# three probes named `attacker` below are the shape the first cut let through:
# a sender who holds a valid DKIM key for a domain of their own, posting a
# message whose envelope sender is somebody else's address.

_MX = "mx.example.test"
_DKIM_OK = f"{_MX}; dkim=pass header.i=@hotel.test header.d=hotel.test"
_SPF_OK = f"{_MX}; spf=pass smtp.mailfrom=hotel.test"


def test_dkim_pass_for_the_senders_own_domain_is_enough():
    assert intake.sender_allowed(_DKIM_OK, "gm@hotel.test", None) is True


def test_spf_pass_for_the_senders_own_domain_is_enough():
    assert intake.sender_allowed(_SPF_OK, "gm@hotel.test", None) is True


def test_neither_passing_is_refused():
    assert intake.sender_allowed(
        f"{_MX}; dkim=fail header.d=hotel.test; spf=softfail "
        f"smtp.mailfrom=hotel.test; dmarc=none",
        "gm@hotel.test", None,
    ) is False


def test_a_missing_authentication_results_header_is_refused():
    assert intake.sender_allowed(None, "gm@hotel.test", None) is False


@pytest.mark.parametrize(
    "auth_result",
    [
        f"{_MX}; dkim=passthrough header.d=hotel.test",
        f"{_MX}; spf=passed smtp.mailfrom=hotel.test",
        f'{_MX}; spf=fail (helo does not match "dkim=pass") smtp.mailfrom=hotel.test',
        f"{_MX}; spf=fail (dkim=pass was claimed) smtp.mailfrom=hotel.test",
        f'{_MX}; dkim=none reason="dkim=pass elsewhere" header.d=hotel.test',
    ],
)
def test_pass_is_matched_as_a_token_not_a_substring(auth_result):
    assert intake.sender_allowed(auth_result, "gm@hotel.test", None) is False


def test_a_nested_comment_grants_no_pass():
    """One substitution pass over `((x) dkim=pass ...)` leaves `( dkim=pass
    ...)`, whose surviving text tokenizes into a live pass. Stripping to a
    fixpoint consumes the whole nest, so only the real `spf=none` is left."""
    payload = "spf=none ((x) dkim=pass header.d=hotel.test )"
    assert [r.method for r in intake._auth_results(payload)] == ["spf"]
    assert intake.sender_allowed(payload, "gm@hotel.test", None) is False


def test_an_unbalanced_comment_yields_no_results():
    """Receivers interpolate the MAIL FROM into the SPF comment, so a crafted
    local part can close the comment early and open another. Where the comment
    ended is then unknowable, and the header is refused whole."""
    payload = "spf=fail (mx: ... ) smtp.helo=x) dkim=pass header.d=hotel.test ("
    assert intake._auth_results(payload) == []
    assert intake.sender_allowed(payload, "gm@hotel.test", None) is False


def test_a_comment_after_a_real_pass_does_not_break_it():
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass (2048-bit key) header.d=hotel.test", "gm@hotel.test", None
    ) is True


# --- the pass must be FOR the envelope sender (D-OH23.4 alignment) -----------


def test_a_dkim_pass_for_someone_elses_domain_is_not_a_pass_for_this_sender():
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=attacker.test", "gm@hotel.test", None
    ) is False


def test_an_spf_pass_for_someone_elses_mailfrom_is_not_a_pass_for_this_sender():
    assert intake.sender_allowed(
        f"{_MX}; spf=pass smtp.mailfrom=attacker.test", "gm@hotel.test", None
    ) is False


def test_an_allowlist_does_not_rescue_an_unaligned_pass():
    """The allowlist reads the envelope sender, which nothing has authenticated
    yet; on its own it would admit a forged MAIL FROM carrying somebody else's
    genuine signature."""
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=attacker.test", "gm@hotel.test", ["hotel.test"]
    ) is False


def test_a_parent_signing_domain_vouches_for_a_subdomain_sender():
    # Relaxed alignment: d=hotel.test signs for gm@mail.hotel.test.
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=hotel.test", "gm@mail.hotel.test", None
    ) is True


def test_a_sibling_signing_domain_does_not_vouch():
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=other.hotel.test", "gm@mail.hotel.test", None
    ) is False


@pytest.mark.parametrize(
    ("signing_domain", "envelope_from"),
    [
        ("nothotel.test", "gm@hotel.test"),
        ("hotel.test", "gm@nothotel.test"),
    ],
)
def test_a_shared_suffix_without_a_label_boundary_does_not_vouch(
    signing_domain, envelope_from
):
    """The bare-`endswith` bug, both ways round."""
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d={signing_domain}", envelope_from, None
    ) is False


def test_spf_alignment_is_exact_with_no_parent_relaxation():
    """SPF authenticates the MAIL FROM itself, so a parent domain's pass is
    not this sender's pass."""
    assert intake.sender_allowed(
        f"{_MX}; spf=pass smtp.mailfrom=hotel.test", "gm@mail.hotel.test", None
    ) is False


@pytest.mark.parametrize(
    "mailfrom", ["hotel.test", "gm@hotel.test", "@hotel.test"]
)
def test_every_spelling_of_smtp_mailfrom_aligns(mailfrom):
    """A receiver may write the property as a bare domain, a whole address, or
    `@domain`; all three name the same domain."""
    assert intake.sender_allowed(
        f"{_MX}; spf=pass smtp.mailfrom={mailfrom}", "gm@hotel.test", None
    ) is True


def test_an_envelope_sender_in_angle_brackets_is_understood():
    """A worker that forwards `message.from` verbatim can hand over
    `<gm@hotel.test>`; leaving the `>` on the domain would refuse it."""
    assert intake.sender_allowed(_DKIM_OK, "<gm@hotel.test>", ["hotel.test"]) is True


def test_an_spf_helo_pass_is_not_a_mailfrom_pass():
    assert intake.sender_allowed(
        f"{_MX}; spf=pass smtp.helo=hotel.test", "gm@hotel.test", None
    ) is False


@pytest.mark.parametrize("auth_result", [f"{_MX}; dkim=pass", f"{_MX}; spf=pass"])
def test_a_pass_carrying_no_domain_proves_nothing(auth_result):
    assert intake.sender_allowed(auth_result, "gm@hotel.test", None) is False


def test_one_aligned_pass_among_several_results_is_enough():
    # A forwarder that adds its own signature leaves two dkim results.
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=forwarder.test; dkim=pass header.d=hotel.test",
        "gm@hotel.test", None,
    ) is True


def test_a_failed_dkim_alongside_an_aligned_spf_passes():
    assert intake.sender_allowed(
        f"{_MX}; dkim=fail header.d=attacker.test; spf=pass smtp.mailfrom=hotel.test",
        "gm@hotel.test", None,
    ) is True


# --- the allowlist, second layer ---------------------------------------------


def test_an_allowlist_refuses_a_domain_outside_it_even_with_an_aligned_pass():
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=elsewhere.test", "gm@elsewhere.test", ["hotel.test"]
    ) is False


def test_an_allowlist_admits_its_own_domain():
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=hotel.test", "GM@Hotel.Test", ["hotel.test"]
    ) is True


def test_an_allowlist_is_exact_and_admits_no_subdomain():
    """The operator lists the sending domain after reading it off the event
    log, so there is nothing to relax — unlike DKIM alignment above."""
    assert intake.sender_allowed(
        f"{_MX}; dkim=pass header.d=hotel.test", "gm@mail.hotel.test", ["hotel.test"]
    ) is False


def test_an_empty_allowlist_behaves_like_no_allowlist():
    """A stored `sender_domains` of `[]` is "no domains configured", not "no
    domain permitted" — the same answer `None` gives."""
    for domains in ([], None):
        assert intake.sender_allowed(_DKIM_OK, "gm@hotel.test", domains) is True
        assert intake.sender_allowed(
            f"{_MX}; dkim=fail header.d=hotel.test", "gm@hotel.test", domains
        ) is False


def test_an_allowlist_does_not_replace_the_authentication_check():
    assert intake.sender_allowed(
        f"{_MX}; dkim=fail header.d=hotel.test; spf=fail smtp.mailfrom=hotel.test",
        "gm@hotel.test", ["hotel.test"],
    ) is False


@pytest.mark.parametrize("envelope_from", ["postmaster", "", "@hotel.test", "<>"])
def test_a_sender_with_no_domain_of_its_own_is_refused(envelope_from):
    assert intake.sender_allowed(_DKIM_OK, envelope_from, None) is False
    assert intake.sender_allowed(_DKIM_OK, envelope_from, ["hotel.test"]) is False


# --- attachments -------------------------------------------------------------

_PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\ntrailer\n"
_XLSX = b"PK\x03\x04\x14\x00\x00\x00\x08\x00fake workbook"


def _message(*parts: tuple[bytes, str, str, str | None]) -> bytes:
    """Build a multipart message; each part is (payload, maintype, subtype, filename)."""
    msg = EmailMessage()
    msg["From"] = "gm@hotel.test"
    msg["To"] = "na-abc@intake.example.test"
    msg["Subject"] = "Night audit"
    msg.set_content("Attached.")
    for payload, maintype, subtype, filename in parts:
        if filename is None:
            msg.add_attachment(payload, maintype=maintype, subtype=subtype)
        else:
            msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def test_only_the_pdf_and_xlsx_parts_come_back():
    raw = _message(
        (_PDF, "application", "pdf", "Trial Balance.pdf"),
        (_XLSX, "application", "vnd.ms-excel", "AR Aging.xlsx"),
        (b"a signature block", "text", "plain", "signature.txt"),
    )
    assert intake.attachments_of(raw) == [
        ("Trial_Balance.pdf", _PDF), ("AR_Aging.xlsx", _XLSX)
    ]


def test_a_part_that_is_neither_format_is_dropped_whatever_it_is_called():
    raw = _message((b"MZ not a pdf at all", "application", "pdf", "evil.pdf"))
    assert intake.attachments_of(raw) == []


def test_an_attachment_with_no_filename_is_named_by_its_magic_bytes():
    raw = _message(
        (_PDF, "application", "pdf", None),
        (_XLSX, "application", "vnd.ms-excel", None),
    )
    assert [name for name, _ in intake.attachments_of(raw)] == [
        "attachment-1.pdf", "attachment-2.xlsx"
    ]


def test_an_attachment_filename_is_scrubbed_to_the_allowed_alphabet():
    raw = _message((_PDF, "application", "pdf", "../../evil report.pdf"))
    (name, _), = intake.attachments_of(raw)
    assert name == ".._.._evil_report.pdf"


@pytest.mark.parametrize("filename", [".", ".."])
def test_a_filename_that_scrubs_away_to_nothing_falls_back(filename):
    """`.` and `..` are exactly what the upload endpoint refuses as a
    filename."""
    raw = _message((_PDF, "application", "pdf", filename))
    assert [name for name, _ in intake.attachments_of(raw)] == ["attachment-1.pdf"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (None, "attachment-3.pdf"),
        ("", "attachment-3.pdf"),
        (".", "attachment-3.pdf"),
        ("..", "attachment-3.pdf"),
        ("/", "_.pdf"),          # a separator scrubs to a usable character
        ("  ", "__.pdf"),
        ("Trial Balance.pdf", "Trial_Balance.pdf"),
        ("x" * 200, "x" * 116 + ".pdf"),
    ],
)
def test_the_attachment_naming_table(filename, expected):
    assert intake._attachment_name(filename, ".pdf", 3) == expected


def test_a_very_long_filename_is_truncated_with_its_extension_kept():
    raw = _message((_PDF, "application", "pdf", "R" * 400 + ".pdf"))
    (name, _), = intake.attachments_of(raw)
    assert len(name) == 120
    assert name.endswith(".pdf")
    assert name[:-4] == "R" * 116


def test_the_extension_comes_from_the_magic_bytes_not_the_filename():
    """A part named `lies.pdf` carrying XLSX bytes comes back `lies.xlsx`: the
    suffix is a claim about the format, and the bytes are the fact."""
    raw = _message(
        (_XLSX, "application", "pdf", "lies.pdf"),
        (_PDF, "application", "vnd.ms-excel", "alsolies.xlsx"),
    )
    assert [name for name, _ in intake.attachments_of(raw)] == [
        "lies.xlsx", "alsolies.pdf"
    ]


def test_a_filename_with_no_extension_gains_one():
    raw = _message((_PDF, "application", "pdf", "trialbalance"))
    assert [name for name, _ in intake.attachments_of(raw)] == ["trialbalance.pdf"]


def test_a_dotfile_name_keeps_its_leading_dot_as_the_stem():
    raw = _message((_PDF, "application", "pdf", ".hidden"))
    assert [name for name, _ in intake.attachments_of(raw)] == [".hidden.pdf"]


def test_a_forwarded_report_is_found():
    """The night auditor forwards the PMS email rather than re-sending the
    file; the report is then inside a `message/rfc822` part."""
    inner = EmailMessage()
    inner["From"] = "pms@vendor.test"
    inner["Subject"] = "Night audit"
    inner.set_content("See attached.")
    inner.add_attachment(
        _PDF, maintype="application", subtype="pdf", filename="Trial Balance.pdf"
    )

    outer = EmailMessage()
    outer["From"] = "gm@hotel.test"
    outer["Subject"] = "Fwd: Night audit"
    outer.set_content("Forwarding.")
    outer.add_attachment(inner)

    assert intake.attachments_of(outer.as_bytes()) == [("Trial_Balance.pdf", _PDF)]


def test_a_message_with_no_attachments_is_an_empty_list():
    msg = EmailMessage()
    msg["From"] = "gm@hotel.test"
    msg.set_content("Where is the report?")
    assert intake.attachments_of(msg.as_bytes()) == []


def test_unparseable_bytes_are_an_empty_list_not_an_exception():
    assert intake.attachments_of(b"\x00\x01\x02 not a message") == []
