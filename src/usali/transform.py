"""Transform staged PMS rows into USALI facts, with mapping exceptions and reconciliation."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from usali.models import (
    MappingException,
    PmsDailyFinancialStage,
    UsaliFinancialFact,
    UsaliMappingDictionary,
)


class ReconciliationError(RuntimeError):
    pass


@dataclass
class TransformResult:
    mapped: int
    unmapped: int
    skipped: int
    reconciled: bool


def transform(
    session: Session, *, source: str, business_date: date, edition: int
) -> TransformResult:
    """Map staged rows for (source, business_date) to USALI facts using the edition's dictionary.

    Unmapped rows are recorded as MappingException rows (nothing is silently dropped).
    Reruns are idempotent: stage rows whose stage_id already has a persisted fact or
    exception are skipped rather than re-inserted. Reconciliation verifies persisted
    totals (facts + exceptions, across all runs) still equal the full staged total.
    """
    stage_rows = (
        session.execute(
            select(PmsDailyFinancialStage).where(
                PmsDailyFinancialStage.pms_source == source,
                PmsDailyFinancialStage.business_date == business_date,
            )
        )
        .scalars()
        .all()
    )

    mappings = {
        m.pms_trx_code: m
        for m in session.execute(
            select(UsaliMappingDictionary).where(
                UsaliMappingDictionary.pms_source == source,
                UsaliMappingDictionary.usali_edition == edition,
            )
        ).scalars()
    }

    # A stage row is "processed" if a prior run produced either a fact or an
    # exception for it — hence the union across both output tables.
    processed_stage_ids: set[int] = set(
        session.execute(
            select(UsaliFinancialFact.stage_id).where(
                UsaliFinancialFact.pms_source == source,
                UsaliFinancialFact.business_date == business_date,
            )
        ).scalars()
    ) | set(
        session.execute(
            select(MappingException.stage_id).where(
                MappingException.pms_source == source,
                MappingException.business_date == business_date,
            )
        ).scalars()
    )

    mapped = unmapped = skipped = 0
    stage_total = Decimal("0")

    for row in stage_rows:
        stage_total += Decimal(row.raw_amount)
        if row.stage_id in processed_stage_ids:
            skipped += 1
            continue
        m = mappings.get(row.pms_trx_code)
        if m is None:
            session.add(
                MappingException(
                    pms_source=source,
                    pms_trx_code=row.pms_trx_code,
                    pms_trx_desc=row.pms_trx_desc,
                    raw_amount=row.raw_amount,
                    business_date=business_date,
                    ingest_batch_id=row.ingest_batch_id,
                    stage_id=row.stage_id,
                )
            )
            unmapped += 1
            continue
        session.add(
            UsaliFinancialFact(
                property_id=row.property_id,
                pms_source=source,
                business_date=business_date,
                usali_edition=edition,
                usali_schedule_id=m.usali_schedule_id,
                usali_major_category=m.usali_major_category,
                usali_sub_category=m.usali_sub_category,
                usali_line_item=m.usali_line_item,
                amount=row.raw_amount,
                room_count=row.room_count,
                gl_account_code=m.gl_account_code,
                ingest_batch_id=row.ingest_batch_id,
                stage_id=row.stage_id,
            )
        )
        mapped += 1

    # Flush so the facts/exceptions just added are visible to the aggregate queries below.
    session.flush()

    fact_total_raw = session.scalar(
        select(func.coalesce(func.sum(UsaliFinancialFact.amount), Decimal("0"))).where(
            UsaliFinancialFact.pms_source == source,
            UsaliFinancialFact.business_date == business_date,
        )
    )
    assert fact_total_raw is not None
    fact_total = Decimal(fact_total_raw)

    exc_total_raw = session.scalar(
        select(func.coalesce(func.sum(MappingException.raw_amount), Decimal("0"))).where(
            MappingException.pms_source == source,
            MappingException.business_date == business_date,
        )
    )
    assert exc_total_raw is not None
    exc_total = Decimal(exc_total_raw)

    # Exact Decimal equality is safe here: all amounts are fixed-precision Numeric(15,4)/Decimal
    # end-to-end (no floats enter this comparison), so there is no rounding drift to tolerate.
    # Do not switch this to a tolerance-based comparison.
    reconciled = stage_total == (fact_total + exc_total)
    if not reconciled:
        raise ReconciliationError(
            f"stage {stage_total} != fact {fact_total} + exceptions {exc_total}"
        )
    return TransformResult(mapped=mapped, unmapped=unmapped, skipped=skipped, reconciled=reconciled)
