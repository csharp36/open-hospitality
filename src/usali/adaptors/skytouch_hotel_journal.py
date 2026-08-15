import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_paren_amount, parse_skytouch_date
from usali.schemas import StagedRecord

_CODE_RE = re.compile(r"^\(([A-Z0-9]{1,6})\)$")
_AMOUNT_RE = re.compile(r"^\(?-?[\d,]+\.\d{2}\)?$")
_TOTAL_LABEL = "Today's Total:"
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def extract_business_date(words: list[Word]) -> date:
    for w in words[:80]:
        if _DATE_RE.match(w.text):
            return parse_skytouch_date(w.text)
    raise ValueError(
        "no SkyTouch M/D/YYYY business date found in Hotel Journal header"
    )


def parse_hotel_journal(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StagedRecord]:
    out: list[StagedRecord] = []
    total_row_sum: Decimal | None = None
    for row in cluster_rows(words, y_tol):
        toks = [w.text for w in sorted(row, key=lambda w: w.x0)]
        joined = " ".join(toks)
        amounts = [t for t in toks if _AMOUNT_RE.match(t)]
        if joined.startswith(_TOTAL_LABEL):
            if amounts:
                total_row_sum = parse_paren_amount(amounts[0])
            continue
        code_idx = next(
            (i for i, t in enumerate(toks) if _CODE_RE.match(t)), None
        )
        if code_idx is None or not amounts:
            continue
        code = _CODE_RE.match(toks[code_idx]).group(1)
        desc = " ".join(toks[:code_idx]).strip() or None
        out.append(
            StagedRecord(
                property_id=property_id,
                pms_source="SKYTOUCH",
                report_type="hotel_journal",
                business_date=business_date,
                pms_trx_code=code,
                pms_trx_desc=desc,
                raw_amount=parse_paren_amount(amounts[0]),  # Postings column
            )
        )
    if total_row_sum is not None:
        got = sum((r.raw_amount for r in out), Decimal("0"))
        if got != total_row_sum:
            raise ValueError(
                f"SkyTouch Hotel Journal postings {got} != Today's Total "
                f"{total_row_sum} — layout changed?"
            )
    return out
