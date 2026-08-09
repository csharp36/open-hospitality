import hashlib
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from usali.models import IngestBatch, PmsLedgerBalanceStage
from usali.schemas import LedgerRecord
from usali.tenancy import current_org_id, is_org_instrumented


def _row_hash(rec: LedgerRecord, file_hash: str, index: int) -> str:
    # Ordinal position keeps two identical (label, kind, amount) rows distinct — the
    # same no-silent-drop rationale as the financial stage hash. The heading-qualified
    # label already separates the sign-opposed "Deposits Transfered at Check-In" pair.
    key = f"{file_hash}|{index}|{rec.ledger_label}|{rec.kind}|{rec.amount}|{rec.report_type}"
    return hashlib.sha256(key.encode()).hexdigest()


def stage_ledgers(
    session: Session,
    records: Sequence[LedgerRecord],
    *,
    batch: IngestBatch,
    source_file: str,
    file_hash: str,
) -> None:
    """Stage ledger block rows under an existing batch.

    Unlike the financial/statistics stagers this does NOT open its own IngestBatch: the
    ledger block rides along with the trial balance's financial rows, and process_file
    guarantees exactly one batch per file.
    """
    # L8-F3/F4: stamp org_id (the Core insert bypasses the write-wall) and add
    # it to the on_conflict target — see usali.stage.stage_records.
    org_stamp = current_org_id(session) if is_org_instrumented(session) else None
    for index, rec in enumerate(records):
        values: dict[str, object] = dict(
            property_id=rec.property_id,
            pms_source=rec.pms_source,
            report_type=rec.report_type,
            business_date=rec.business_date,
            ledger_label=rec.ledger_label,
            amount=rec.amount,
            kind=rec.kind,
            source_file=source_file,
            ingest_batch_id=batch.batch_id,
            row_hash=_row_hash(rec, file_hash, index),
        )
        if org_stamp is not None:
            values["org_id"] = org_stamp
        stmt = insert(PmsLedgerBalanceStage).values(**values).on_conflict_do_nothing(
            index_elements=["org_id", "pms_source", "business_date", "row_hash"]
        )
        session.execute(stmt)
