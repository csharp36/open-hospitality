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
    saw_total = False
    for row in cluster_rows(words, y_tol):
        toks = [w.text for w in sorted(row, key=lambda w: w.x0)]
        joined = " ".join(toks)
        if joined.startswith(_TOTAL_LABEL):
            saw_total = True
            tot_amounts = [t for t in toks if _AMOUNT_RE.match(t)]
            if tot_amounts:
                total_row_sum = parse_paren_amount(tot_amounts[0])
            continue
        code_idx = next(
            (i for i, t in enumerate(toks) if _CODE_RE.match(t)), None
        )
        if code_idx is None:
            continue
        # Postings is the first amount column AFTER the code — anchor by column
        # position, never by "first amount-shaped token in the row", so a rate/unit
        # token in the description can't be mistaken for the money.
        amounts = [t for t in toks[code_idx + 1:] if _AMOUNT_RE.match(t)]
        if not amounts:
            continue
        code_match = _CODE_RE.match(toks[code_idx])
        assert code_match is not None  # code_idx was found via the same regex
        code = code_match.group(1)
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
    # Fail CLOSED: an absent total row means the parse is unverifiable, so refuse it
    # rather than returning unreconciled records.
    if not saw_total:
        raise ValueError(
            "no Today's Total row in Hotel Journal — cannot verify the parse"
        )
    if total_row_sum is None:
        raise ValueError(
            "Today's Total row found but its amount could not be parsed"
        )
    # Reconcile against the Postings total (the day's GROSS activity by column), not
    # Totals (the net-after-corrections roll-up): raw_amount is the Postings column,
    # so the staged sum must equal the Postings total.
    got = sum((r.raw_amount for r in out), Decimal("0"))
    if got != total_row_sum:
        raise ValueError(
            f"SkyTouch Hotel Journal postings {got} != Today's Total "
            f"{total_row_sum} — layout changed?"
        )
    return out
