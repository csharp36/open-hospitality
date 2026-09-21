"""OH-23: the night-audit report validator shared by every path that accepts
a file.

The cases here run the same `usali.night_audit_validation` entry points the
upload endpoint calls. The endpoint's own behavior — status codes, the
business-date refusal it keeps, what stages — is pinned by
tests/test_night_audit.py.
"""

from pathlib import Path

import pytest

from usali import intake, night_audit_validation as validation

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
    assert validation.validate_for_property(db_session, data, "HISJ", "OPERA") is None


def test_the_opera_flash_is_wrong_property_for_another_property(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf").read_bytes()
    assert validation.validate_for_property(
        db_session, data, "SSSJ", "AUTOCLERK"
    ) == "wrong_property"


def test_the_skytouch_pack_validates_through_the_pack_path(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf").read_bytes()
    assert validation.validate_for_property(db_session, data, "STDEMO", "SKYTOUCH") is None


def test_the_skytouch_pack_is_wrong_property_for_another_property(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf").read_bytes()
    assert validation.validate_for_property(
        db_session, data, "HISJ", "OPERA"
    ) == "wrong_property"


def test_a_pdf_of_garbage_is_unreadable(db_session):
    """`unreadable` is for bytes the READER cannot turn into words: these
    never reach detection at all (pdfminer: "No /Root object!")."""
    _seed_registry(db_session)
    assert validation.validate_for_property(
        db_session, b"%PDF-1.4 and then nothing that parses", "HISJ", "OPERA"
    ) == "unreadable"


def test_a_readable_report_no_property_claims_is_a_detection_failure(db_session):
    """The other half of the split: the words parse, so the document is not
    `unreadable`; no registered property resolves, so it is not a night-audit
    report for anyone here."""
    data = (_SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf").read_bytes()
    # Deliberately no _seed_registry: the detection registry is empty.
    assert validation.validate_single(
        db_session, data, "HISJ", "OPERA"
    ).outcome == "not_a_night_audit_report"
    assert validation.validate_for_property(
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
    checked = validation.validate_sections(db_session, sections, "STDEMO", "SKYTOUCH")
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
    checked = validation.validate_sections(db_session, sections, "STDEMO", "OPERA")
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
    """Which branch `validate_document` takes. The single-report branch is
    `_validate_detected`, which the probe calls directly with the words and
    detection it already has; `validate_single` funnels through the same
    function, so patching it catches both arrivals."""
    taken: list[str] = []

    def _pack(*_a, **_k):
        taken.append("pack")
        return validation.Validation(None, "")

    def _single(*_a, **_k):
        taken.append("single")
        return validation.Validation(None, "")

    monkeypatch.setattr(validation, "validate_pack", _pack)
    monkeypatch.setattr(validation, "_validate_detected", _single)
    validation.validate_document(session, data, "STDEMO", "SKYTOUCH")
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
    assert validation.validate_for_property(
        db_session, data, "SSSJ", "OPERA"
    ) == "not_a_night_audit_report"


def test_the_rate_plan_is_fine_for_its_own_source(db_session):
    _seed_registry(db_session)
    data = (_SAMPLES / "Autoclerk - Revenue by Rate Plan 07.07.2026.pdf").read_bytes()
    assert validation.validate_for_property(db_session, data, "SSSJ", "AUTOCLERK") is None


@pytest.mark.parametrize(
    ("sample", "reads"),
    [
        ("Manager Flash 07.07.2026 - Opera.pdf", 1),
        ("Autoclerk - Revenue by Rate Plan 07.07.2026.pdf", 1),
    ],
    ids=["flash", "rate_plan"],
)
def test_a_single_report_is_read_once(db_session, monkeypatch, sample, reads):
    """`validate_document` probes single-report detection and then validates.
    It used to throw the probe's work away and let `validate_single` read,
    load the registry and detect all over again — double the work on a
    document that can be 25 MB."""
    real_read = validation.read_words_from_bytes
    calls: list[int] = []

    def _counting(data):
        calls.append(1)
        return real_read(data)

    monkeypatch.setattr(validation, "read_words_from_bytes", _counting)
    _seed_registry(db_session)
    data = (_SAMPLES / sample).read_bytes()
    assert validation.validate_for_property(db_session, data, "SSSJ", "AUTOCLERK") in (
        None, "wrong_property"
    )
    assert len(calls) == reads


def test_every_validator_outcome_is_one_the_event_table_accepts():
    """`validate_for_property` may only answer with outcomes the intake event
    row can actually record; the frozenset itself is pinned against the CHECK
    in tests/test_intake_schema.py::
    test_intake_outcomes_match_the_check_constraint."""
    assert validation.VALIDATION_OUTCOMES <= intake.INTAKE_OUTCOMES
