import hashlib
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from usali.models import IngestBatch, PmsDailySegmentStage
from usali.schemas import SegmentRecord
from usali.tenancy import current_org_id, is_org_instrumented


def _row_hash(rec: SegmentRecord, file_hash: str, index: int) -> str:
    # Ordinal position keeps two identical (code, measure, period, value) rows distinct — the
    # same no-silent-drop rationale as the financial stage hash.
    key = (
        f"{file_hash}|{index}|{rec.segment_code}|{rec.measure}|"
        f"{rec.period_label}|{rec.value}|{rec.report_type}"
    )
    return hashlib.sha256(key.encode()).hexdigest()


def stage_segments(
    session: Session, records: Sequence[SegmentRecord], *, source_file: str, file_hash: str
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
    session.flush()

    # L8-F3/F4: stamp org_id (the Core insert bypasses the write-wall) and add
    # it to the on_conflict target — see usali.stage.stage_records.
    org_stamp = current_org_id(session) if is_org_instrumented(session) else None
    for index, rec in enumerate(records):
        values: dict[str, object] = dict(
            property_id=rec.property_id,
            pms_source=rec.pms_source,
            report_type=rec.report_type,
            business_date=rec.business_date,
            segment_code=rec.segment_code,
            segment_desc=rec.segment_desc,
            measure=rec.measure,
            period_label=rec.period_label,
            value=rec.value,
            source_file=source_file,
            ingest_batch_id=batch.batch_id,
            row_hash=_row_hash(rec, file_hash, index),
        )
        if org_stamp is not None:
            values["org_id"] = org_stamp
        stmt = insert(PmsDailySegmentStage).values(**values).on_conflict_do_nothing(
            index_elements=["org_id", "pms_source", "business_date", "row_hash"]
        )
        session.execute(stmt)
    return batch
