import hashlib
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from usali.models import IngestBatch, PmsDailyFinancialStage
from usali.schemas import StagedRecord
from usali.tenancy import current_org_id, is_org_instrumented


def _row_hash(rec: StagedRecord, file_hash: str, index: int) -> str:
    # Include the row's ordinal position within the parsed batch so that two genuinely
    # distinct lines sharing the same (code, amount, section) are NOT collapsed by the
    # unique constraint (which would silently drop financial data). Re-ingesting the same
    # file stays idempotent because the parser emits rows in a deterministic order, so a
    # given line keeps the same index — and therefore the same hash — across runs.
    key = f"{file_hash}|{index}|{rec.pms_trx_code}|{rec.raw_amount}|{rec.section}|{rec.report_type}"
    return hashlib.sha256(key.encode()).hexdigest()


def stage_records(
    session: Session, records: Sequence[StagedRecord], *, source_file: str, file_hash: str
) -> IngestBatch:
    source = records[0].pms_source if records else "UNKNOWN"
    report_type = records[0].report_type if records else "unknown"
    batch = IngestBatch(
        pms_source=source,
        report_type=report_type,
        source_file=source_file,
        file_hash=file_hash,
        status="staged",
        row_count=len(records),
    )
    session.add(batch)
    session.flush()  # assign batch_id

    # L8-F3: the Core insert() below BYPASSES the L6a before_flush write-wall,
    # so org_id would fall to the server default '1' and an org != 1 session's
    # rows would die on the DB wall's WITH CHECK (raw RLS 500). Stamp it from
    # the one-predicate source, exactly as the write wall does — but only when
    # the session is org-instrumented (a bound session stamps its org; an
    # instrumented-but-unbound one refuses loudly via MissingOrgContext;
    # owner/superuser seeds stay on the server default). L8-F4: org_id also
    # joins the on_conflict target so a cross-org row_hash collision keeps both.
    org_stamp = current_org_id(session) if is_org_instrumented(session) else None
    for index, rec in enumerate(records):
        values: dict[str, object] = dict(
            property_id=rec.property_id,
            pms_source=rec.pms_source,
            report_type=rec.report_type,
            business_date=rec.business_date,
            pms_trx_code=rec.pms_trx_code,
            pms_trx_desc=rec.pms_trx_desc,
            raw_amount=rec.raw_amount,
            room_count=rec.room_count,
            section=rec.section,
            source_file=source_file,
            ingest_batch_id=batch.batch_id,
            row_hash=_row_hash(rec, file_hash, index),
        )
        if org_stamp is not None:
            values["org_id"] = org_stamp
        stmt = insert(PmsDailyFinancialStage).values(**values).on_conflict_do_nothing(
            index_elements=["org_id", "pms_source", "business_date", "row_hash"]
        )
        session.execute(stmt)
    return batch
