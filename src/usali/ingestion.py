"""Single entry point for the file lifecycle: detect -> parse -> stage -> transform -> file.

Handles both financial reports (trial balance, transaction summary) and statistics
reports (manager flash, manager's report) through a uniform per-report handler table
keyed by (pms_source, report_type).

`process_bytes`, `process_pack_bytes` and `process_document_bytes` are the core: they
take the uploaded bytes and a name, and never touch the caller's file. The path
functions (`process_file`, `process_pack`, `process_document`) read the file and
delegate; they do not move or delete it.

Any failure in the DB pipeline rolls back the in-flight transaction, records a `failed`
IngestBatch (with the error message), files an error record with no content to
`failed_dir` (`retention.write_error_record`), and re-raises as ProcessingError. Success
commits, then files ONE redacted words artifact to `processed_dir`
(`retention.build_artifact` + `write_artifact`) as a separate phase — a filing failure
after the commit never fabricates a `failed` batch, because the data is committed.
Exactly one IngestBatch row is produced per `process_bytes` call, regardless of outcome.

`process_pack_bytes` splits a bundled night-audit pack and ingests each recognized
section under a shared transaction, producing one IngestBatch per recognized section on
success (still exactly one `failed` batch on failure, with the whole transaction rolled
back) and one artifact holding the recognized sections, with the skipped section titles
named in `sections_dropped`.
"""

import dataclasses
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.adaptors import autoclerk_manager_report as mgr
from usali.adaptors import autoclerk_rate_plan as rate_plan
from usali.adaptors import autoclerk_transaction_summary as autoclerk
from usali.adaptors import hotelkey as hk
from usali.adaptors import hotelkey_all_payments as hk_payments
from usali.adaptors import hotelkey_ar_aging as hk_ar
from usali.adaptors import hotelkey_hotel_statistics as hk_stats
from usali.adaptors import hotelkey_settlement as hk_settlement
from usali.adaptors import opera_manager_flash as flash
from usali.adaptors import opera_market_stats as market_stats
from usali.adaptors import opera_trial_balance as opera
from usali.adaptors import skytouch_hotel_journal as sky_journal
from usali.adaptors import skytouch_hotel_statistics as sky_stats
from usali.adaptors.pack import split_pack
from usali.adaptors.pdf import Word, extract_pages_from_bytes
from usali.adaptors.reader import is_pdf, read_words_from_bytes
from usali.detect import Detection, detect, load_registry
from usali import gl_posting
from usali.ledger_promote import promote_ledgers
from usali.ledger_stage import stage_ledgers
from usali.models import IngestBatch, IngestionCoverage
from usali.redaction import mask_pans
from usali.retention import (
    RetainedSection,
    build_artifact,
    write_artifact,
    write_error_record,
)
from usali.segment_promote import promote_segments
from usali.segment_stage import stage_segments
from usali.stage import stage_records
from usali.stats_promote import promote_statistics
from usali.stats_stage import stage_statistics
from usali.transform import transform


class ProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    pms_source: str
    report_type: str
    property_id: str
    business_date: date
    staged: int
    mapped: int
    unmapped: int
    skipped: int
    destination: Path  # the filed artifact path


@dataclass(frozen=True)
class _Counts:
    staged: int
    mapped: int
    unmapped: int
    skipped: int


_Handler = Callable[
    [Session, list[Word], Detection, Path, str, int], tuple[IngestBatch, date, _Counts]
]


def _run_opera_trial_balance(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = opera.extract_business_date(words)
    records = opera.parse_trial_balance(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_records(session, records, source_file=path.name, file_hash=file_hash)
    r = transform(session, source=det.pms_source, business_date=business_date, edition=edition)
    # The trial balance also carries the ledger reconciliation block (guest/AR/deposit/
    # package balances). Stage + promote it in the same transaction, under the same
    # batch — counts (incl. batch.row_count) stay financial-only so the result contract
    # is unchanged; ledger stage rows simply share the batch.
    ledgers = opera.parse_trial_balance_ledgers(
        words, property_id=det.property_id, business_date=business_date
    )
    if not ledgers:
        # A trial balance ALWAYS has a ledger block. Zero rows means the block anchor
        # wasn't found — fail loud through the quarantine path rather than letting the
        # A/R report quietly degrade to no data.
        raise ValueError(
            "trial balance has no ledger reconciliation block — Opera format changed?"
        )
    stage_ledgers(session, ledgers, batch=batch, source_file=path.name, file_hash=file_hash)
    promote_ledgers(
        session, "mapping/ledgers.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(records), r.mapped, r.unmapped, r.skipped)


def _run_autoclerk_transaction_summary(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = autoclerk.extract_business_date(words)
    records = autoclerk.parse_transaction_summary(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_records(session, records, source_file=path.name, file_hash=file_hash)
    r = transform(session, source=det.pms_source, business_date=business_date, edition=edition)
    return batch, business_date, _Counts(len(records), r.mapped, r.unmapped, r.skipped)


def _run_opera_manager_flash(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = opera.extract_business_date(words)  # same MM-DD-YY header format
    records = flash.parse_manager_flash(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_statistics(session, records, source_file=path.name, file_hash=file_hash)
    r = promote_statistics(
        session, "mapping/statistics.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(records), r.promoted, 0, r.skipped)


def _run_autoclerk_manager_report(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = mgr.extract_business_date(words)
    records = mgr.parse_manager_report(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_statistics(session, records, source_file=path.name, file_hash=file_hash)
    r = promote_statistics(
        session, "mapping/statistics.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(records), r.promoted, 0, r.skipped)


def _run_opera_market_stats(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = opera.extract_business_date(words)  # same MM-DD-YY header format
    records = market_stats.parse_market_stats(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_segments(session, records, source_file=path.name, file_hash=file_hash)
    r = promote_segments(
        session, "mapping/segments.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(records), r.promoted_segments, 0, r.skipped)


def _run_autoclerk_rate_plan(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = rate_plan.extract_business_date(words)
    records = rate_plan.parse_rate_plan(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_segments(session, records, source_file=path.name, file_hash=file_hash)
    r = promote_segments(
        session, "mapping/segments.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(records), r.promoted_segments, 0, r.skipped)


def _run_skytouch_hotel_journal(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = sky_journal.extract_business_date(words)
    records = sky_journal.parse_hotel_journal(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_records(session, records, source_file=path.name, file_hash=file_hash)
    r = transform(session, source=det.pms_source, business_date=business_date, edition=edition)
    return batch, business_date, _Counts(len(records), r.mapped, r.unmapped, r.skipped)


def _run_skytouch_hotel_statistics(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = sky_stats.extract_business_date(words)
    records = sky_stats.parse_hotel_statistics(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_statistics(session, records, source_file=path.name, file_hash=file_hash)
    r = promote_statistics(
        session, "mapping/statistics.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(records), r.promoted, 0, r.skipped)


def _open_batch(
    session: Session, det: Detection, path: Path, file_hash: str, row_count: int
) -> IngestBatch:
    """An IngestBatch named from the DETECTION, for handlers whose stager does not
    open one (ledger-only files) or that stage financial rows -- `stage_records`
    names its own batch from the first record and would label an empty one
    UNKNOWN/unknown."""
    batch = IngestBatch(
        pms_source=det.pms_source,
        report_type=det.report_type,
        source_file=path.name,
        file_hash=file_hash,
        status="staged",
        row_count=row_count,
    )
    session.add(batch)
    session.flush()
    return batch


def _run_hotelkey_hotel_statistics(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = hk.extract_business_date(words)
    stats = hk_stats.parse_hotel_statistics(
        words, property_id=det.property_id, business_date=business_date
    )
    financial = hk_stats.parse_financial_rows(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = stage_statistics(session, stats, source_file=path.name, file_hash=file_hash)
    # D-OH22.6: the revenue, tax and payment lines ride under the same batch and
    # are NOT transformed -- no usali_financial_fact row, so gl_posting finds no
    # plan and writes nothing. Held from this side by
    # tests/test_hotelkey_end_to_end.py::test_hotelkey_never_produces_financial_facts_or_journal_entries
    # and from the CLI's `transform --source HOTELKEY` by
    # tests/test_hotelkey_end_to_end.py::test_the_transform_cli_cannot_promote_hotelkey_rows
    # (transform() refuses a source in SOURCE_NOTICES).
    stage_records(session, financial, source_file=path.name, file_hash=file_hash, batch=batch)
    batch.row_count += len(financial)
    r = promote_statistics(
        session, "mapping/statistics.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(stats) + len(financial), r.promoted, 0, r.skipped)


def _run_hotelkey_settlement(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = hk.extract_business_date(words)
    records = hk_settlement.parse_settlement(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = _open_batch(session, det, path, file_hash, len(records))
    stage_records(session, records, source_file=path.name, file_hash=file_hash, batch=batch)
    return batch, business_date, _Counts(len(records), 0, 0, 0)


def _run_hotelkey_all_payments(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = hk.extract_business_date(words)
    records = hk_payments.parse_all_payments(
        words, property_id=det.property_id, business_date=business_date
    )
    batch = _open_batch(session, det, path, file_hash, len(records))
    stage_records(session, records, source_file=path.name, file_hash=file_hash, batch=batch)
    return batch, business_date, _Counts(len(records), 0, 0, 0)


def _run_hotelkey_ar_aging(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> tuple[IngestBatch, date, _Counts]:
    business_date = hk.extract_business_date(words)
    records = hk_ar.parse_ar_aging(words, property_id=det.property_id, business_date=business_date)
    batch = _open_batch(session, det, path, file_hash, len(records))
    stage_ledgers(session, records, batch=batch, source_file=path.name, file_hash=file_hash)
    r = promote_ledgers(
        session, "mapping/ledgers.yaml", source=det.pms_source, business_date=business_date
    )
    return batch, business_date, _Counts(len(records), r.promoted, 0, r.skipped)


_PIPELINES: dict[tuple[str, str], _Handler] = {
    ("OPERA", "trial_balance"): _run_opera_trial_balance,
    ("AUTOCLERK", "transaction_summary"): _run_autoclerk_transaction_summary,
    ("OPERA", "manager_flash"): _run_opera_manager_flash,
    ("AUTOCLERK", "manager_report"): _run_autoclerk_manager_report,
    ("OPERA", "market_stats"): _run_opera_market_stats,
    ("AUTOCLERK", "rate_plan"): _run_autoclerk_rate_plan,
    ("SKYTOUCH", "hotel_journal"): _run_skytouch_hotel_journal,
    ("SKYTOUCH", "hotel_statistics"): _run_skytouch_hotel_statistics,
    ("HOTELKEY", "hotel_statistics"): _run_hotelkey_hotel_statistics,
    ("HOTELKEY", "settlement"): _run_hotelkey_settlement,
    ("HOTELKEY", "all_payments"): _run_hotelkey_all_payments,
    ("HOTELKEY", "ar_aging"): _run_hotelkey_ar_aging,
}


def record_coverage(
    session: Session, property_id: str, business_date: date, report_type: str
) -> None:
    """Idempotent: one coverage row per (property, business_date, report_type).
    ORM get-or-create so the org_id before_flush stamp applies."""
    exists = session.execute(
        select(IngestionCoverage.coverage_id).where(
            IngestionCoverage.property_id == property_id,
            IngestionCoverage.business_date == business_date,
            IngestionCoverage.report_type == report_type,
        )
    ).scalar_one_or_none()
    if exists is None:
        session.add(IngestionCoverage(property_id=property_id, business_date=business_date,
                                      report_type=report_type))


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_message(exc: Exception) -> str:
    """The one place an exception's text becomes a stored or returned message.

    An adapter refusal names the row and column it refused rather than echoing
    cells (adaptors/hotelkey_xlsx._refusal, adaptors/autoclerk_rate_plan), which
    is where report content is kept out of these messages. This is the second
    line, for an exception from anywhere: the text reaches IngestBatch.message,
    the filed error record and the upload's HTTP body, so a card number in it is
    masked first (tests/test_ingestion_boundary.py::
    test_a_card_number_in_an_exception_never_reaches_the_batch_or_the_record).
    """
    return mask_pans(str(exc))[:500]


def process_bytes(
    session: Session,
    data: bytes,
    name: str,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> ProcessResult:
    """Detect, parse, stage, transform, and file one PDF or XLSX held in memory.

    The property detection registry is read from the DB (`load_registry`), not from
    `mapping/properties.yaml` — properties must be seeded via `seed_properties` before
    files can be processed. On success the IngestBatch is marked "transformed", the
    transaction commits, and ONE redacted words artifact is filed to `processed_dir`
    (`retention.build_artifact` + `write_artifact`). On a pipeline failure the
    transaction is rolled back, a `failed` IngestBatch is recorded with the error
    message, an error record carrying no content is filed to `failed_dir`, and a
    ProcessingError is raised (chained). A filing failure AFTER the commit raises
    ProcessingError but leaves the committed data untouched.
    """
    try:
        words = read_words_from_bytes(data)
        det = detect(words, load_registry(session))
        result = _process_section(session, words, det, Path(name), _hash(data), edition)
        session.commit()
    except Exception as exc:
        session.rollback()
        _record_failure(session, name, data, exc)
        write_error_record(failed_dir, source_file=name, data=data, error=_safe_message(exc))
        raise ProcessingError(f"{name}: {_safe_message(exc)}") from exc

    section = RetainedSection(
        title=name,
        pms_source=det.pms_source,
        report_type=det.report_type,
        property_id=det.property_id,
        business_date=result.business_date,
        words=words,
    )
    return dataclasses.replace(
        result,
        destination=_file_artifact(
            processed_dir, name, data, kind="single", sections=[section], sections_dropped=[]
        ),
    )


def _file_artifact(
    processed_dir: Path,
    name: str,
    data: bytes,
    *,
    kind: str,
    sections: list[RetainedSection],
    sections_dropped: list[str],
) -> Path:
    """Build and write the redacted artifact, post-commit.

    Filing is a separate phase from the transaction: the data is already committed, so
    a failure here must NOT fabricate a `failed` batch or roll anything back. Every
    failure — retention's own (a policy whose kept columns are absent) as well as the
    filesystem's — becomes a ProcessingError naming that the data is committed, so
    nothing but ProcessingError escapes a post-commit filing. Pinned by
    tests/test_ingestion.py::test_filing_failure_after_commit_does_not_fabricate_failed_batch.
    """
    try:
        artifact = build_artifact(
            source_file=name,
            data=data,
            kind=kind,
            sections=sections,
            sections_dropped=sections_dropped,
        )
        return write_artifact(processed_dir, artifact)
    except Exception as exc:
        raise ProcessingError(
            f"{name}: data committed, but filing to {processed_dir} failed: {exc}"
        ) from exc


def process_file(
    session: Session,
    pdf_path: str | Path,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> ProcessResult:
    """Read one PDF or XLSX and run it through `process_bytes`.

    The file at `path` is never moved or deleted; the caller owns it. What is filed is
    the redacted artifact (success) or the error record (failure) — see `process_bytes`.
    """
    p = Path(pdf_path)
    return process_bytes(
        session, p.read_bytes(), p.name,
        processed_dir=processed_dir, failed_dir=failed_dir, edition=edition,
    )


def _process_section(
    session: Session, words: list[Word], det: Detection, path: Path, file_hash: str, edition: int
) -> ProcessResult:
    """Run one detected report through its handler + coverage, marking the batch
    transformed. Shared by `process_bytes` (single report) and `process_pack_bytes`
    (each section). Neither commits nor files — the caller owns the transaction and
    the artifact. `path` carries the source file NAME for the handlers' `source_file`.
    The returned `destination` is a placeholder; the caller fixes it up."""
    handler = _PIPELINES[(det.pms_source, det.report_type)]
    batch, business_date, counts = handler(session, words, det, path, file_hash, edition)
    # Record which report type landed for this property-day. A file that spans
    # DAY/MONTH/YEAR periods records the DAY business_date the handler returns.
    record_coverage(session, det.property_id, business_date, det.report_type)
    batch.status = "transformed"
    # OH-27: promotion and posting land in the caller's one transaction.
    # post_and_record's docstring is the contract enforced here: the typed
    # GL refusals become failed ledger rows rather than exceptions, so
    # a books problem never quarantines a parsed file (what deliberately
    # still escapes is IntegrityError and invariant violations); with no
    # chart seeded it returns "skipped" and writes nothing (GL is off).
    gl_posting.post_and_record(
        session,
        property_id=det.property_id,
        business_date=business_date,
        source_type="pms_daily",
        actor="ingestion",
    )
    return ProcessResult(
        pms_source=det.pms_source,
        report_type=det.report_type,
        property_id=det.property_id,
        business_date=business_date,
        staged=counts.staged,
        mapped=counts.mapped,
        unmapped=counts.unmapped,
        skipped=counts.skipped,
        destination=path,  # placeholder; caller replaces with the filed artifact
    )


def process_pack_bytes(
    session: Session,
    data: bytes,
    name: str,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> list[ProcessResult]:
    """Split a bundled night-audit pack held in memory into its constituent reports
    and ingest each recognised section under a shared transaction.

    Unknown sections (housekeeping/filler like A/R Aging, or a report with no registered
    handler) are skipped: their words are discarded at the boundary and only their titles
    are kept, in the artifact's `sections_dropped`. All recognised sections stage +
    transform in one transaction: any failure rolls the whole pack back, records a
    `failed` IngestBatch, files an error record to `failed_dir`, and re-raises as
    ProcessingError. A pack with no recognised sections is itself a failure. On success
    the transaction commits and ONE artifact holding the recognised sections is filed to
    `processed_dir`; every result's `destination` is that artifact.
    """
    file_hash = _hash(data)
    results: list[ProcessResult] = []
    retained: list[RetainedSection] = []
    dropped: list[str] = []
    try:
        sections = split_pack(extract_pages_from_bytes(data))
        registry = load_registry(session)
        for section in sections:
            try:
                # Pass the section TITLE: it, not the 120-word header window,
                # decides the report signature. Several SkyTouch reports print a
                # `Rate Plan` COLUMN, which the window matched against the
                # AutoClerk `rate_plan` signature (issue #78).
                det = detect(section.words, registry, section.title)
            except ValueError:
                dropped.append(section.title or "(untitled)")
                continue  # unknown report or unresolved property (housekeeping/filler)
            if (det.pms_source, det.report_type) not in _PIPELINES:
                dropped.append(section.title or "(untitled)")
                continue
            result = _process_section(
                session, section.words, det, Path(name), file_hash, edition
            )
            results.append(result)
            retained.append(
                RetainedSection(
                    title=section.title,
                    pms_source=det.pms_source,
                    report_type=det.report_type,
                    property_id=det.property_id,
                    business_date=result.business_date,
                    words=section.words,
                )
            )
        if not results:
            raise ValueError("no recognized report sections in pack")
        session.commit()
    except Exception as exc:
        session.rollback()
        _record_failure(session, name, data, exc)
        write_error_record(failed_dir, source_file=name, data=data, error=_safe_message(exc))
        raise ProcessingError(f"{name}: {_safe_message(exc)}") from exc

    dest = _file_artifact(
        processed_dir, name, data, kind="pack", sections=retained, sections_dropped=dropped
    )
    return [dataclasses.replace(r, destination=dest) for r in results]


def process_pack(
    session: Session,
    pdf_path: str | Path,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> list[ProcessResult]:
    """Read one bundled night-audit pack and run it through `process_pack_bytes`.

    The file at `path` is never moved or deleted; the caller owns it. What is filed is
    the redacted artifact (success) or the error record (failure).
    """
    p = Path(pdf_path)
    return process_pack_bytes(
        session, p.read_bytes(), p.name,
        processed_dir=processed_dir, failed_dir=failed_dir, edition=edition,
    )


def process_document_bytes(
    session: Session,
    data: bytes,
    name: str,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> list[ProcessResult]:
    """Ingest one file whose shape is not known in advance: a single report
    (PDF or XLSX) or a bundled night-audit pack (PDF only).

    Single-report detection is tried first; only a ValueError from `detect`
    (nothing matched, no property resolved, or the first matching signature
    disagrees with the property's registered source) selects the pack path.
    The rule is calibrated on the committed samples, not proved: a pack bound
    with a cleanly detectable report first passes the probe and is processed
    as a single report, and a single report whose header window matches an
    earlier signature than its title takes the pack path. Both shapes and the
    reasoning are in docs/design/2026-09-20-seed-pack-routing-design.md, D1.

    The pack path is not taken for `Autoclerk - Manager Report 07.07.2026.pdf`,
    the multi-page single report that `split_pack` carves into four sections;
    pinned in tests/test_process_document.py::test_single_report_never_takes_the_pack_path,
    with the split itself pinned in
    tests/adaptors/test_pack.py::test_manager_report_splits_into_four_sections_one_resolvable.

    Reading the bytes is not a routing signal: if it raises, the file takes
    the single-report path, and `process_bytes` records the failed batch and
    files the error record (tests/test_process_document.py::
    test_a_corrupt_pdf_is_quarantined_by_the_single_report_path).
    When the pack path fails too, the raised ProcessingError names both
    reasons; the failed batch and the error record were already recorded by
    `process_pack_bytes`.
    """
    if is_pdf(data):
        try:
            words = read_words_from_bytes(data)
        except Exception:
            words = None  # not a routing signal; process_bytes owns the failure
        if words is not None:
            try:
                detect(words, load_registry(session))
            except ValueError as single_exc:
                try:
                    return process_pack_bytes(session, data, name, processed_dir=processed_dir,
                                              failed_dir=failed_dir, edition=edition)
                except ProcessingError as pack_exc:
                    raise ProcessingError(
                        f"{name}: as a single report: {single_exc}; "
                        f"as a pack: {pack_exc}"
                    ) from pack_exc
    return [process_bytes(session, data, name, processed_dir=processed_dir,
                          failed_dir=failed_dir, edition=edition)]


def process_document(
    session: Session,
    path: str | Path,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> list[ProcessResult]:
    """Read one file of unknown shape and run it through `process_document_bytes`.

    The file at `path` is never moved or deleted; the caller owns it. What is filed is
    the redacted artifact (success) or the error record (failure).
    """
    p = Path(path)
    return process_document_bytes(
        session, p.read_bytes(), p.name,
        processed_dir=processed_dir, failed_dir=failed_dir, edition=edition,
    )


def _record_failure(session: Session, name: str, data: bytes, exc: Exception) -> None:
    batch = IngestBatch(
        pms_source="UNKNOWN",
        report_type="unknown",
        source_file=name,
        file_hash=_hash(data),
        status="failed",
        message=_safe_message(exc),
    )
    session.add(batch)
    session.commit()
