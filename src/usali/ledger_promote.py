import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import PmsLedgerBalanceStage, UsaliLedgerBalanceFact

_LOG = logging.getLogger(__name__)


class LedgerMapping(BaseModel):
    source: str
    label: str
    code: str
    name: str
    # Curated kind — the A/R report filters on fact.kind, so it must come from the
    # mapping, not from the parser's "label contains 'Balance'" heuristic.
    kind: Literal["balance", "activity"]


@dataclass
class LedgerPromoteResult:
    promoted: int
    ignored: int   # uncurated label — intentionally stage-only (lenient, like statistics)
    skipped: int   # already promoted on a prior run


def _load_ledger_map(path: str | Path) -> dict[tuple[str, str], LedgerMapping]:
    rows = [LedgerMapping(**row) for row in yaml.safe_load(Path(path).read_text())]
    return {(m.source, m.label): m for m in rows}


def promote_ledgers(
    session: Session, mapping_path: str | Path, *, source: str, business_date: date
) -> LedgerPromoteResult:
    ledger_map = _load_ledger_map(mapping_path)
    stage_rows = session.execute(
        select(PmsLedgerBalanceStage).where(
            PmsLedgerBalanceStage.pms_source == source,
            PmsLedgerBalanceStage.business_date == business_date,
        )
    ).scalars().all()
    already = set(
        session.execute(
            select(UsaliLedgerBalanceFact.ledger_stage_id).where(
                UsaliLedgerBalanceFact.pms_source == source,
                UsaliLedgerBalanceFact.business_date == business_date,
            )
        ).scalars()
    )

    promoted = ignored = skipped = 0
    for row in stage_rows:
        if row.ledger_stage_id in already:
            skipped += 1
            continue
        mapping = ledger_map.get((source, row.ledger_label))
        if mapping is None:
            ignored += 1
            continue
        if row.kind != mapping.kind:
            # Facts carry the curated kind; the staged row keeps the parser heuristic
            # for informational display. A disagreement means either the heuristic or
            # the curation is stale — surface it, but don't block promotion.
            _LOG.warning(
                "ledger kind disagreement for %r: mapping says %s, heuristic says %s "
                "— promoting with the curated kind",
                row.ledger_label, mapping.kind, row.kind,
            )
        session.add(UsaliLedgerBalanceFact(
            property_id=row.property_id, pms_source=source, business_date=business_date,
            ledger_code=mapping.code, ledger_name=mapping.name, kind=mapping.kind,
            amount=row.amount, ingest_batch_id=row.ingest_batch_id,
            ledger_stage_id=row.ledger_stage_id,
        ))
        promoted += 1
    return LedgerPromoteResult(promoted=promoted, ignored=ignored, skipped=skipped)
