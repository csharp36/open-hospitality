from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import PmsDailyStatisticStage, UsaliStatisticFact

# Canonical reporting periods, keyed by the label each adapter stages. Opera
# and AutoClerk stage DAY/Today, MONTH/MTD, YEAR/YTD; SkyTouch stages its
# column headings (ACTUAL, PTD, LY_PTD, YTD, LY_YTD). is_prior_year is copied
# from the stage row unchanged, so LY_PTD lands as (MTD, prior); that the
# adapter sets it on the LY_* rows is pinned in
# tests/adaptors/test_skytouch_hotel_statistics.py::test_source_report_type_and_prior_year_flag.
# Anything not listed (Yesterday, Tomorrow, ...) stays stage-only by design;
# the model is lenient, these are KPIs not ledger data. The SkyTouch label set
# is pinned against this table in
# tests/test_stats_promote.py::test_every_skytouch_period_label_is_canonical.
_CANONICAL_PERIODS = {
    "DAY": "DAY", "Today": "DAY", "ACTUAL": "DAY",
    "MONTH": "MTD", "MTD": "MTD", "PTD": "MTD", "LY_PTD": "MTD",
    "YEAR": "YTD", "YTD": "YTD", "LY_YTD": "YTD",
}


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
