"""Single entry point for the file lifecycle: detect -> parse -> stage -> transform -> file.

Handles both financial reports (trial balance, transaction summary) and statistics
reports (manager flash, manager's report) through a uniform per-report handler table
keyed by (pms_source, report_type).

Any failure in the DB pipeline rolls back the in-flight transaction, records a `failed`
IngestBatch (with the error message), quarantines the source file to `failed_dir`, and
re-raises as ProcessingError. Success commits, then moves the source file to
`processed_dir` as a separate phase — a filing failure after the commit leaves the file
in place (retry is an idempotent no-op) and never fabricates a `failed` batch. Exactly
one IngestBatch row is produced per call, regardless of outcome.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from usali.adaptors import autoclerk_manager_report as mgr
from usali.adaptors import autoclerk_rate_plan as rate_plan
from usali.adaptors import autoclerk_transaction_summary as autoclerk
from usali.adaptors import opera_manager_flash as flash
from usali.adaptors import opera_market_stats as market_stats
from usali.adaptors import opera_trial_balance as opera
from usali.adaptors.pdf import Word, extract_words
from usali.detect import Detection, detect, load_registry
from usali.ledger_promote import promote_ledgers
from usali.ledger_stage import stage_ledgers
from usali.models import IngestBatch
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
    destination: Path


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


_PIPELINES: dict[tuple[str, str], _Handler] = {
    ("OPERA", "trial_balance"): _run_opera_trial_balance,
    ("AUTOCLERK", "transaction_summary"): _run_autoclerk_transaction_summary,
    ("OPERA", "manager_flash"): _run_opera_manager_flash,
    ("AUTOCLERK", "manager_report"): _run_autoclerk_manager_report,
    ("OPERA", "market_stats"): _run_opera_market_stats,
    ("AUTOCLERK", "rate_plan"): _run_autoclerk_rate_plan,
}


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _move(path: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / path.name
    if dest.exists():  # same name already filed: disambiguate with the content hash
        dest = target_dir / f"{path.stem}.{_file_hash(path)[:8]}{path.suffix}"
    path.rename(dest)
    return dest


def process_file(
    session: Session,
    pdf_path: str | Path,
    *,
    processed_dir: Path,
    failed_dir: Path,
    edition: int = 12,
) -> ProcessResult:
    """Detect, parse, stage, transform, and file one PDF.

    The property detection registry is read from the DB (`load_registry`), not from
    `mapping/properties.yaml` — properties must be seeded via `seed_properties` before
    files can be processed. On success the IngestBatch is marked "transformed", the
    transaction commits, and the file moves to `processed_dir`. On a pipeline failure the
    transaction is rolled back, a `failed` IngestBatch is recorded with the error message,
    the file is quarantined to `failed_dir`, and a ProcessingError is raised (chained). A
    filing failure AFTER the commit raises ProcessingError but leaves the file and the
    committed data untouched.
    """
    path = Path(pdf_path)
    try:
        words = extract_words(path)
        det = detect(words, load_registry(session))
        handler = _PIPELINES[(det.pms_source, det.report_type)]
        batch, business_date, counts = handler(
            session, words, det, path, _file_hash(path), edition
        )
        batch.status = "transformed"
        session.commit()
    except Exception as exc:
        session.rollback()
        _record_failure(session, path, exc)
        _move(path, failed_dir)
        raise ProcessingError(f"{path.name}: {exc}") from exc

    # Post-commit filing is a separate phase: the data is already committed, so a move
    # failure must NOT fabricate a `failed` batch or quarantine the file. Leaving the
    # file in place is safe — a retry is an idempotent no-op that re-attempts the move.
    try:
        dest = _move(path, processed_dir)
    except OSError as exc:
        raise ProcessingError(
            f"{path.name}: data committed, but filing to {processed_dir} failed: {exc}"
        ) from exc
    return ProcessResult(
        pms_source=det.pms_source,
        report_type=det.report_type,
        property_id=det.property_id,
        business_date=business_date,
        staged=counts.staged,
        mapped=counts.mapped,
        unmapped=counts.unmapped,
        skipped=counts.skipped,
        destination=dest,
    )


def _record_failure(session: Session, path: Path, exc: Exception) -> None:
    batch = IngestBatch(
        pms_source="UNKNOWN",
        report_type="unknown",
        source_file=path.name,
        file_hash=_file_hash(path) if path.exists() else "",
        status="failed",
        message=str(exc)[:500],
    )
    session.add(batch)
    session.commit()
