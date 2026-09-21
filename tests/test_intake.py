"""OH-23 Task 2: the intake primitives and the shared night-audit validator.

The validator cases below run the same `usali.intake` entry points the upload
endpoint now calls; the upload endpoint's own behavior (status codes, the
business-date refusal it keeps, what stages) stays pinned by
tests/test_night_audit.py, which this task left untouched.
"""

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

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


@pytest.mark.parametrize("skew", [0, 299, -299])
def test_a_timestamp_inside_the_window_is_accepted(skew):
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


def test_an_spf_mailfrom_written_as_a_whole_address_still_aligns():
    assert intake.sender_allowed(
        f"{_MX}; spf=pass smtp.mailfrom=gm@hotel.test", "gm@hotel.test", None
    ) is True


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


@pytest.mark.parametrize("envelope_from", ["postmaster", "", "@hotel.test"])
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


def test_an_attachment_filename_is_scrubbed_with_safe_component():
    raw = _message((_PDF, "application", "pdf", "../../evil report.pdf"))
    (name, _), = intake.attachments_of(raw)
    assert name == intake._safe_component("../../evil report.pdf")
    assert "/" not in name


def test_a_message_with_no_attachments_is_an_empty_list():
    msg = EmailMessage()
    msg["From"] = "gm@hotel.test"
    msg.set_content("Where is the report?")
    assert intake.attachments_of(msg.as_bytes()) == []


def test_unparseable_bytes_are_an_empty_list_not_an_exception():
    assert intake.attachments_of(b"\x00\x01\x02 not a message") == []


# --- the shared validator ----------------------------------------------------

_SAMPLES = Path("docs/reference/samples")


def _seed_registry(db_session):
    from tests.authkit import DEFAULT_ORG_ALIAS
    from usali.mapping.property_registry import seed_properties
    from usali.models import Organization

    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.commit()
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()


def test_the_opera_flash_validates_for_its_own_property(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf").read_bytes()
    assert intake.validate_for_property(db_session, data, "HISJ", "OPERA") is None


def test_the_opera_flash_is_wrong_property_for_another_property(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf").read_bytes()
    assert intake.validate_for_property(
        db_session, data, "SSSJ", "AUTOCLERK"
    ) == "wrong_property"


def test_the_skytouch_pack_validates_through_the_pack_path(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf").read_bytes()
    assert intake.validate_for_property(db_session, data, "STDEMO", "SKYTOUCH") is None


def test_the_skytouch_pack_is_wrong_property_for_another_property(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf").read_bytes()
    assert intake.validate_for_property(
        db_session, data, "HISJ", "OPERA"
    ) == "wrong_property"


def test_a_pdf_of_garbage_is_unreadable(db_session):
    """`unreadable` is for bytes the READER cannot turn into words: these
    never reach detection at all (pdfminer: "No /Root object!")."""
    _seed_registry(db_session)
    assert intake.validate_for_property(
        db_session, b"%PDF-1.4 and then nothing that parses", "HISJ", "OPERA"
    ) == "unreadable"


def test_a_readable_report_no_property_claims_is_a_detection_failure(db_session):
    """The other half of the split: the words parse, so the document is not
    `unreadable`; no registered property resolves, so it is not a night-audit
    report for anyone here."""
    data = (_SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf").read_bytes()
    # Deliberately no _seed_registry: the detection registry is empty.
    assert intake.validate_single(
        db_session, data, "HISJ", "OPERA"
    ).outcome == "not_a_night_audit_report"
    assert intake.validate_for_property(
        db_session, data, "HISJ", "OPERA"
    ) == "not_a_night_audit_report"


def test_a_pack_whose_sections_match_no_signature_is_a_detection_failure(db_session):
    """No section title matches a report signature, so every section is
    skipped and the pack is not a night audit — not `unreadable`, which would
    claim the bytes never parsed."""
    from usali.adaptors.pack import ReportSection
    from usali.adaptors.pdf import Word

    _seed_registry(db_session)
    sections = [ReportSection(
        title="Housekeeping Discrepancy",
        words=[Word(text=t, x0=float(10 * i), top=10.0)
               for i, t in enumerate(["Housekeeping", "Discrepancy", "Redstone", "Test", "Inn"])],
    )]
    checked = intake.validate_sections(db_session, sections, "STDEMO", "SKYTOUCH")
    assert checked.outcome == "not_a_night_audit_report"
    assert checked.detail == "no recognized report sections in this pack"
    # A refusal carries no sections and no titles: `Validation` documents that
    # a caller holding an outcome has nothing further to do with the document.
    assert checked.sections == () and checked.skipped_titles == ()


def test_a_pack_section_outside_the_required_set_is_refused_by_report_type(db_session):
    """The refusal `night_audit_api._ingest_pack` gained when it moved onto the
    shared validator: the pack path checks the report TYPE per recognized
    section, not only the property. The wording is asserted in full because
    it is what the endpoint raises as its 422."""
    from usali.adaptors.pack import split_pack
    from usali.adaptors.pdf import extract_pages_from_bytes

    _seed_registry(db_session)
    data = (_SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf").read_bytes()
    sections = split_pack(extract_pages_from_bytes(data))
    # STDEMO's own sections, asked against OPERA's required set.
    checked = intake.validate_sections(db_session, sections, "STDEMO", "OPERA")
    assert checked.outcome == "not_a_night_audit_report"
    assert checked.detail == (
        "pack section 'Hotel Journal Summary': hotel_journal is not part of "
        "this property's night audit (OPERA requires: manager_flash, "
        "market_stats, trial_balance)"
    )
    assert checked.sections == () and checked.skipped_titles == ()


class _TookPath(Exception):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _validator_path(session, data, monkeypatch) -> str:
    taken: list[str] = []

    def _pack(*_a, **_k):
        taken.append("pack")
        return intake.Validation(None, "")

    def _single(*_a, **_k):
        taken.append("single")
        return intake.Validation(None, "")

    monkeypatch.setattr(intake, "validate_pack", _pack)
    monkeypatch.setattr(intake, "validate_single", _single)
    intake.validate_document(session, data, "STDEMO", "SKYTOUCH")
    return taken[0]


def _ingest_path(session, data, tmp_path, monkeypatch) -> str:
    from usali import ingestion

    def _pack(*_a, **_k):
        raise _TookPath("pack")

    def _single(*_a, **_k):
        raise _TookPath("single")

    monkeypatch.setattr(ingestion, "process_pack_bytes", _pack)
    monkeypatch.setattr(ingestion, "process_bytes", _single)
    with pytest.raises(_TookPath) as excinfo:
        ingestion.process_document_bytes(
            session, data, "probe.pdf",
            processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
        )
    return excinfo.value.path


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ("SkyTouch - Standard Audit Pack (mock).pdf", "pack"),
        ("Manager Flash 07.07.2026 - Opera.pdf", "single"),
    ],
    ids=["pack", "single"],
)
def test_routing_agrees_with_the_ingest_path(
    db_session, tmp_path, monkeypatch, sample, expected
):
    """`validate_document` and `ingestion.process_document_bytes` must not
    disagree about a document's SHAPE — a validator that read a pack as a
    single report would pass judgement on one section and then watch the
    ingest stage two. Both are asked over the same two committed samples,
    which go opposite ways. The ingest side's own rule is pinned by
    tests/test_process_document.py::test_single_report_never_takes_the_pack_path
    and ::test_pack_sample_routes_to_the_pack_path."""
    _seed_registry(db_session)
    data = (_SAMPLES / sample).read_bytes()
    assert _validator_path(db_session, data, monkeypatch) == expected
    assert _ingest_path(db_session, data, tmp_path, monkeypatch) == expected


def test_a_report_outside_the_nights_required_set_is_refused(db_session):
    """The AutoClerk rate plan resolves to SSSJ, so the property check passes;
    `rate_plan` is not one of OPERA's three required reports, so the report-type
    check is what refuses it."""
    _seed_registry(db_session)
    data = (_SAMPLES / "Autoclerk - Revenue by Rate Plan 07.07.2026.pdf").read_bytes()
    assert intake.validate_for_property(
        db_session, data, "SSSJ", "OPERA"
    ) == "not_a_night_audit_report"


def test_the_rate_plan_is_fine_for_its_own_source(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "Autoclerk - Revenue by Rate Plan 07.07.2026.pdf").read_bytes()
    assert intake.validate_for_property(db_session, data, "SSSJ", "AUTOCLERK") is None


def test_every_validator_outcome_is_one_the_event_table_accepts():
    """`validate_for_property` may only answer with outcomes the intake event
    row can actually record; the frozenset itself is pinned against the CHECK
    in tests/test_intake_schema.py::
    test_intake_outcomes_match_the_check_constraint."""
    assert intake.VALIDATION_OUTCOMES <= intake.INTAKE_OUTCOMES
