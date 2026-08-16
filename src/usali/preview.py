from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from usali.schemas import StagedRecord

# mapping/*.yaml live at the repo root (src/usali/preview.py -> parents[2] == repo root).
_MAPPING_DIR = Path(__file__).resolve().parents[2] / "mapping"


@dataclass(frozen=True)
class PnlLine:
    major: str
    sub: str
    line_item: str
    amount: Decimal


@dataclass(frozen=True)
class Kpi:
    label: str
    value: Decimal


@dataclass(frozen=True)
class PreviewPayload:
    # net_total is the raw sum of the transaction rows — informational only, NOT
    # a balance claim. A real "ties out" signal must come from reconciling the
    # ledger block (a later plan); parse_trial_balance does not return that block,
    # so a ties-out signal must never be derived from the transaction-row net.
    pms_source: str
    report_type: str
    business_date: date
    pnl_lines: list[PnlLine]
    kpis: list[Kpi] = field(default_factory=list)
    codes_recognized: int = 0
    codes_mapped: int = 0
    codes_needs_review: int = 0
    net_total: Decimal = Decimal("0")


@lru_cache(maxsize=None)
def _load_mapping(source: str, edition: int) -> dict[str, dict[str, Any]]:
    path = _MAPPING_DIR / f"{source.lower()}.yaml"
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return {
        str(r["code"]): r
        for r in rows
        if r.get("source") == source and r.get("edition") == edition
    }


def build_financial_preview(
    *,
    source: str,
    report_type: str,
    business_date: date,
    records: list[StagedRecord],
    edition: int = 12,
) -> PreviewPayload:
    mapping = _load_mapping(source, edition)

    buckets: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    seen: set[str] = set()
    mapped: set[str] = set()
    needs_review: set[str] = set()
    net_total = Decimal("0")

    for r in records:
        code = r.pms_trx_code
        seen.add(code)
        net_total += r.raw_amount
        m = mapping.get(code)
        if m is None:
            needs_review.add(code)
            continue
        mapped.add(code)
        if m.get("review_status") == "needs-review":
            needs_review.add(code)
        key = (m["major"], m.get("sub") or "", m["line_item"])
        buckets[key] += r.raw_amount

    pnl_lines = [
        PnlLine(major=k[0], sub=k[1], line_item=k[2], amount=v)
        for k, v in sorted(buckets.items())
    ]
    return PreviewPayload(
        pms_source=source,
        report_type=report_type,
        business_date=business_date,
        pnl_lines=pnl_lines,
        codes_recognized=len(seen),
        codes_mapped=len(mapped),
        codes_needs_review=len(needs_review),
        net_total=net_total,
    )
