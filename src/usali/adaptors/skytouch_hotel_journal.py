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
_POSTINGS_HEADER = "Postings"
_DEFAULT_HALF_SPACING = 35.0  # used only if the header has a lone column anchor


def extract_business_date(words: list[Word]) -> date:
    for w in words[:80]:
        if _DATE_RE.match(w.text):
            return parse_skytouch_date(w.text)
    raise ValueError(
        "no SkyTouch M/D/YYYY business date found in Hotel Journal header"
    )


def _column_anchors(rows: list[list[Word]]) -> list[float]:
    """Header-derived money-column x0s, anchored on the "Postings" label.

    The Hotel Journal emits a column-header row (``Postings Corrections Adjustments
    Totals ...``). We locate the "Postings" header Word and treat it plus every header
    Word at or right of it as the ordered value-column anchors, with Postings at index 0.
    Deriving the anchors from the report itself (rather than a hard-coded x0) lets the
    parse adapt to the real sample's layout. If there is NO "Postings" header the parse
    is unanchorable, so we fail CLOSED — same discipline as the missing-total guard.
    """
    for row in rows:
        for w in row:
            if w.text == _POSTINGS_HEADER:
                anchors = sorted(c.x0 for c in row if c.x0 >= w.x0)
                return anchors
    raise ValueError(
        "no 'Postings' column header in SkyTouch Hotel Journal — cannot anchor the "
        "money column"
    )


def _nearest_column(x0: float, anchors: list[float]) -> int:
    # NEAREST anchor by x0 (mirrors skytouch_hotel_statistics._nearest_column): the
    # column an amount belongs to is the header it sits closest to, so a value never
    # gets pinned to the wrong period/column by ordinal position.
    return min(range(len(anchors)), key=lambda i: abs(anchors[i] - x0))


def _postings_amount(amount_words: list[Word], anchors: list[float]) -> Word | None:
    """The row's Postings-column amount, or None if the Postings cell is empty.

    An amount qualifies only when (a) its nearest column IS Postings (index 0) AND
    (b) it sits within half the Postings->next-column spacing of the Postings anchor.
    Guard (a) rejects a value that lives in Corrections/Totals; guard (b) rejects a
    stray amount-shaped token far to the left (e.g. a rate in the description). A
    corrections-only line therefore contributes NO Postings record — we skip it rather
    than promoting the Corrections value into the Postings column.
    """
    postings_x0 = anchors[0]
    half_spacing = (
        (anchors[1] - anchors[0]) / 2 if len(anchors) > 1 else _DEFAULT_HALF_SPACING
    )
    best: Word | None = None
    for w in amount_words:
        if _nearest_column(w.x0, anchors) != 0:
            continue
        if abs(w.x0 - postings_x0) >= half_spacing:
            continue
        if best is None or abs(w.x0 - postings_x0) < abs(best.x0 - postings_x0):
            best = w
    return best


def parse_hotel_journal(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StagedRecord]:
    rows = cluster_rows(words, y_tol)
    anchors = _column_anchors(rows)  # fails closed if no "Postings" header
    out: list[StagedRecord] = []
    total_postings: Decimal | None = None
    saw_total = False
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        toks = [w.text for w in cells]
        joined = " ".join(toks)
        amount_words = [w for w in cells if _AMOUNT_RE.match(w.text)]
        if joined.startswith(_TOTAL_LABEL):
            saw_total = True
            # Anchor the total the same way as the data rows: the Postings-column amount,
            # never "the first amount token in the row".
            posting = _postings_amount(amount_words, anchors)
            if posting is not None:
                total_postings = parse_paren_amount(posting.text)
            # NOTE: a real multi-page journal that repeats "Today's Total:" on every page
            # would overwrite total_postings with the LAST page's value while `got` below
            # sums records from ALL pages — verify against a real sample. The mock is
            # single-page, so this is safe here.
            continue
        code_idx = next(
            (i for i, t in enumerate(toks) if _CODE_RE.match(t)), None
        )
        if code_idx is None:
            continue
        posting = _postings_amount(amount_words, anchors)
        if posting is None:
            # Empty Postings cell (e.g. a corrections-only line): this row simply has no
            # Postings record. Do NOT fall back to another column.
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
                raw_amount=parse_paren_amount(posting.text),  # Postings column
            )
        )
    # Fail CLOSED: an absent total row means the parse is unverifiable, so refuse it
    # rather than returning unreconciled records.
    if not saw_total:
        raise ValueError(
            "no Today's Total row in Hotel Journal — cannot verify the parse"
        )
    if total_postings is None:
        raise ValueError(
            "Today's Total row found but its Postings amount could not be parsed"
        )
    # Reconcile against the Postings total (the day's GROSS activity in that column):
    # raw_amount is the Postings column, so the staged sum must equal the Postings total.
    got = sum((r.raw_amount for r in out), Decimal("0"))
    if got != total_postings:
        raise ValueError(
            f"SkyTouch Hotel Journal postings {got} != Today's Total "
            f"{total_postings} — layout changed?"
        )
    return out
