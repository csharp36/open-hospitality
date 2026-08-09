from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import PmsDailySegmentStage, UsaliSegmentFact

_CANONICAL_PERIODS = {"DAY": "DAY", "MONTH": "MTD", "YEAR": "YTD"}
_CORE_MEASURES = ("ROOMS", "ROOM_REVENUE")


class SegmentMappingError(RuntimeError):
    pass


class SegmentReconciliationError(RuntimeError):
    pass


class SegmentMapping(BaseModel):
    source: str
    code: str
    segment: str
    confidence: str = "MEDIUM"
    review_status: str = "unreviewed"
    notes: str | None = None


@dataclass
class SegmentPromoteResult:
    promoted_segments: int
    skipped: int
    ignored: int   # non-canonical period or non-core measure — intentionally stage-only


def _load_segment_map(path: str | Path) -> dict[tuple[str, str], str]:
    rows = [SegmentMapping(**row) for row in yaml.safe_load(Path(path).read_text())]
    return {(m.source, m.code): m.segment for m in rows}


def promote_segments(
    session: Session, mapping_path: str | Path, *, source: str, business_date: date
) -> SegmentPromoteResult:
    """Aggregate staged segment rows into usali_segment_fact — STRICTLY, per property.

    Unlike statistics promotion, this is revenue attribution: every staged code must be
    mapped, and per period the mapped segments' ROOMS and ROOM_REVENUE must sum exactly
    to the report's own TOTAL row. A (source, business_date) report can carry rows for
    multiple properties (see mapping/properties.yaml) — each property is promoted
    independently: its own idempotency check, its own strict mapping, its own
    aggregation, and its own TOTAL-row reconciliation. One property's data never mixes
    into another's, and one property being already-promoted never masks another
    property's pending rows. Idempotent per property: an already-promoted
    (source, property, date) is a skip no-op for that property only.
    """
    stage_rows = session.execute(
        select(PmsDailySegmentStage).where(
            PmsDailySegmentStage.pms_source == source,
            PmsDailySegmentStage.business_date == business_date,
        )
    ).scalars().all()

    by_property: dict[str, list[PmsDailySegmentStage]] = {}
    for r in stage_rows:
        by_property.setdefault(r.property_id, []).append(r)

    segment_map = _load_segment_map(mapping_path)

    promoted = skipped = ignored = 0
    for property_id in sorted(by_property):
        property_rows = by_property[property_id]

        existing = session.execute(
            select(UsaliSegmentFact.segment_fact_id).where(
                UsaliSegmentFact.pms_source == source,
                UsaliSegmentFact.property_id == property_id,
                UsaliSegmentFact.business_date == business_date,
            ).limit(1)
        ).first()
        if existing is not None:
            skipped += len(property_rows)
            continue

        unmapped = sorted({
            r.segment_code for r in property_rows
            if r.segment_code != "TOTAL" and (source, r.segment_code) not in segment_map
        })
        if unmapped:
            raise SegmentMappingError(
                f"unmapped segment codes for {source} property {property_id}: "
                f"{', '.join(unmapped)} — "
                "add them to mapping/segments.yaml (promotion is strict by design)"
            )

        # sums[(segment, canonical_period)][measure]; totals[canonical_period][measure]
        sums: dict[tuple[str, str], dict[str, Decimal]] = {}
        totals: dict[str, dict[str, Decimal]] = {}
        batch_id: int | None = None
        property_ignored = 0
        for r in property_rows:
            period = _CANONICAL_PERIODS.get(r.period_label)
            if period is None or r.measure not in _CORE_MEASURES:
                property_ignored += 1
                continue
            value = Decimal(str(r.value))
            if r.segment_code == "TOTAL":
                totals.setdefault(period, {}).setdefault(r.measure, Decimal("0"))
                totals[period][r.measure] += value
                continue
            segment = segment_map[(source, r.segment_code)]
            bucket = sums.setdefault((segment, period), {m: Decimal("0") for m in _CORE_MEASURES})
            bucket[r.measure] += value
            batch_id = r.ingest_batch_id

        if not sums:
            ignored += property_ignored
            continue

        periods = {p for _, p in sums}
        for period in sorted(periods):
            if period not in totals:
                raise SegmentReconciliationError(
                    f"no TOTAL row staged for property {property_id} period {period}; "
                    "cannot verify the split"
                )
            for measure in _CORE_MEASURES:
                seg_sum = sum(
                    (bucket[measure] for (seg, p), bucket in sums.items() if p == period),
                    Decimal("0"),
                )
                total = totals[period].get(measure, Decimal("0"))
                if seg_sum != total:
                    raise SegmentReconciliationError(
                        f"property {property_id} {period} {measure}: "
                        f"segment sum {seg_sum} != report TOTAL {total}"
                    )

        assert batch_id is not None
        for (segment, period), bucket in sorted(sums.items()):
            session.add(UsaliSegmentFact(
                property_id=property_id, pms_source=source, business_date=business_date,
                usali_segment=segment, period=period,
                rooms=bucket["ROOMS"], room_revenue=bucket["ROOM_REVENUE"],
                ingest_batch_id=batch_id,
            ))
        promoted += len(sums)
        ignored += property_ignored

    return SegmentPromoteResult(promoted_segments=promoted, skipped=skipped, ignored=ignored)
