"""The night-audit report validator, shared by every path that accepts a file.

Two checks over one document: does it detect as THIS property, and is what it
detects one of the night's required reports for this PMS. They are the checks
`night_audit_api.upload_night_audit_report` and `night_audit_api._ingest_pack`
run before anything stages, lifted out of those two functions so a second
caller can run them over the same bytes and refuse for the same reasons.

The BUSINESS-DATE check is deliberately NOT here: the upload endpoint keeps it
(D-OH23.5 says an emailed backfill is the point), and it is why the endpoint
asks for `sections` back rather than only an outcome.

This module reads the database (the property detection registry) and pulls in
the PDF/XLSX adaptors; `usali.intake`, which the unauthenticated webhook
imports, deliberately does neither.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args

from sqlalchemy.orm import Session

from usali.adaptors.pack import ReportSection, split_pack
from usali.adaptors.pdf import Word, extract_pages_from_bytes
from usali.adaptors.reader import is_pdf, read_words_from_bytes
from usali.detect import Detection, detect, load_registry
from usali.night_audit import REQUIRED_REPORTS

# The outcomes the validator can answer with, closed IN THE TYPE: mypy --strict
# refuses a fourth string at every `Validation(...)` site, so the set cannot
# drift by someone inventing an outcome the intake event table has no room for.
# The frozenset is derived from the Literal rather than written twice, and its
# containment in `intake.INTAKE_OUTCOMES` is asserted by
# tests/test_night_audit_validation.py::
# test_every_validator_outcome_is_one_the_event_table_accepts.
ValidationOutcome = Literal["wrong_property", "not_a_night_audit_report", "unreadable"]
VALIDATION_OUTCOMES: frozenset[str] = frozenset(get_args(ValidationOutcome))


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


def _read_and_detect(
    session: Session, data: bytes
) -> tuple[list[Word], Detection] | Validation:
    """Words and detection for one single report, or the Validation that
    refuses it.

    The two failures the original inline block treated alike are separated:
    bytes the READER cannot turn into words are `unreadable`, while words that
    no report signature claims, or whose property is not registered, are a
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
    return words, det


def _validate_detected(
    words: list[Word], det: Detection, property_id: str, pms_source: str
) -> Validation:
    """The two checks, over a report that has already been read and detected.

    Both single-report entry points funnel through here, so a SINGLE report is
    read, the registry loaded, and `detect` run once however it arrived — the
    probe in `validate_document` hands its work forward rather than making
    `validate_single` repeat it. A pack is a different shape and pays
    differently: `validate_sections` loads the registry again and detects once
    per section.
    """
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


def validate_single(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> Validation:
    """The single-report checks: readable, this property, a required report."""
    read = _read_and_detect(session, data)
    if isinstance(read, Validation):
        return read
    words, det = read
    return _validate_detected(words, det, property_id, pms_source)


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
    not parse answers `unreadable` exactly as the single-report path does.

    The probe's own words and detection are handed to `_validate_detected`, so
    a single report is read and detected once rather than twice; pinned by
    tests/test_night_audit_validation.py::
    test_a_single_report_is_read_once.

    This rule is written to match the one `ingestion.process_document_bytes`
    routes on, so the validator and the ingest that follows it do not disagree
    about a document's shape. That agreement is pinned by
    tests/test_night_audit_validation.py::test_routing_agrees_with_the_ingest_path,
    over the two committed samples that go opposite ways.
    """
    if not is_pdf(data):
        return validate_single(session, data, property_id, pms_source)
    try:
        words = read_words_from_bytes(data)
    except Exception as exc:
        return Validation("unreadable", f"could not read report: {exc}", cause=exc)
    try:
        det = detect(words, load_registry(session))
    except ValueError:
        return validate_pack(session, data, property_id, pms_source)
    except Exception as exc:
        return Validation("unreadable", f"could not read report: {exc}", cause=exc)
    return _validate_detected(words, det, property_id, pms_source)


def validate_for_property(
    session: Session, data: bytes, property_id: str, pms_source: str
) -> ValidationOutcome | None:
    """None when the document is a night-audit report for this property, else
    one of VALIDATION_OUTCOMES."""
    return validate_document(session, data, property_id, pms_source).outcome
