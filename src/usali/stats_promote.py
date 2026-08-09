from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import PmsDailyStatisticStage, UsaliStatisticFact

# Canonical reporting periods. Anything not listed (Yesterday, Tomorrow, ...) stays
# stage-only by design — the model is lenient, these are KPIs not ledger data.
_CANONICAL_PERIODS = {"DAY": "DAY", "Today": "DAY", "MONTH": "MTD", "MTD": "MTD",
                      "YEAR": "YTD", "YTD": "YTD"}


class MetricMapping(BaseModel):
    source: str
    label: str
    code: str


@dataclass
class PromoteResult:
    promoted: int
    ignored: int   # uncurated label or non-canonical period — intentionally stage-only
    skipped: int   # already promoted on a prior run


def _load_metric_map(path: str | Path) -> dict[tuple[str, str], str]:
    rows = [MetricMapping(**row) for row in yaml.safe_load(Path(path).read_text())]
    return {(m.source, m.label): m.code for m in rows}


def promote_statistics(
    session: Session, mapping_path: str | Path, *, source: str, business_date: date
) -> PromoteResult:
    metric_map = _load_metric_map(mapping_path)
    stage_rows = session.execute(
        select(PmsDailyStatisticStage).where(
            PmsDailyStatisticStage.pms_source == source,
            PmsDailyStatisticStage.business_date == business_date,
        )
    ).scalars().all()
    already = set(
        session.execute(
            select(UsaliStatisticFact.stat_stage_id).where(
                UsaliStatisticFact.pms_source == source,
                UsaliStatisticFact.business_date == business_date,
            )
        ).scalars()
    )

    promoted = ignored = skipped = 0
    for row in stage_rows:
        if row.stat_stage_id in already:
            skipped += 1
            continue
        code = metric_map.get((source, row.metric_label))
        period = _CANONICAL_PERIODS.get(row.period_label)
        if code is None or period is None:
            ignored += 1
            continue
        session.add(UsaliStatisticFact(
            property_id=row.property_id, pms_source=source, business_date=business_date,
            metric_code=code, period=period, is_prior_year=row.is_prior_year,
            value=row.value, ingest_batch_id=row.ingest_batch_id,
            stat_stage_id=row.stat_stage_id,
        ))
        promoted += 1
    return PromoteResult(promoted=promoted, ignored=ignored, skipped=skipped)
