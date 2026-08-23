"""Parse the SkyTouch "Hotel Statistics" report into per-period StatisticRecords.

CALIBRATED TO THE SYNTHETIC MOCK FIXTURE. The column anchors are located by
matching five CLEAN single-word tokens (``PTD PTD1 LYPTD YTD LYYTD``) sitting at the
five value-column x0 positions. A REAL SkyTouch "Hotel Statistics" header instead
wraps MULTI-WORD labels ("Last Year PTD", "Last YTD"), which will not equal these
single tokens. Consequently ``_column_anchors`` will raise ``ValueError`` on a real
sample until it is re-calibrated against a real (de-identified) file. That failure is
deliberate and loud so a future real-sample failure is understood rather than
mysterious: the fix is to re-derive the anchor tokens from an actual header.
"""

import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_skytouch_date
from usali.schemas import StatisticRecord

_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")
_PERIODS = ["ACTUAL", "PTD", "LY_PTD", "YTD", "LY_YTD"]
_ANCHOR_TOKENS = ["PTD", "PTD1", "LYPTD", "YTD", "LYYTD"]
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def extract_business_date(words: list[Word]) -> date:
    for w in words[:80]:
        if _DATE_RE.match(w.text):
            return parse_skytouch_date(w.text)
    raise ValueError("no SkyTouch M/D/YYYY business date in Hotel Statistics header")


def _column_anchors(rows: list[list[Word]]) -> list[float]:
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        texts = [w.text for w in cells]
        for j in range(len(texts) - 4):
            if texts[j : j + 5] == _ANCHOR_TOKENS:
                return [cells[j + k].x0 for k in range(5)]
    raise ValueError(
        "SkyTouch Hotel Statistics column header (PTD PTD1 LYPTD YTD LYYTD) not found"
    )


def _nearest_column(x0: float, anchors: list[float]) -> int:
    # NEAREST anchor by x0 (like opera_manager_flash): sparse rows populate only some
    # value columns with no placeholder tokens, so pairing values to periods by ordinal
    # position would misalign; the ~80pt anchor gaps absorb the left/right drift of
    # wide values comfortably.
    return min(range(len(anchors)), key=lambda i: abs(anchors[i] - x0))


def parse_hotel_statistics(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StatisticRecord]:
    rows = cluster_rows(words, y_tol)
    anchors = _column_anchors(rows)
    label_boundary = anchors[0] - 20  # labels sit left of the first value column
    out: list[StatisticRecord] = []
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        label = " ".join(w.text for w in cells if w.x0 < label_boundary).strip()
        values = [w for w in cells if w.x0 >= label_boundary and _NUM_RE.match(w.text)]
        if not label or not values:
            continue
        for w in values:
            period = _PERIODS[_nearest_column(w.x0, anchors)]
            out.append(
                StatisticRecord(
                    property_id=property_id,
                    pms_source="SKYTOUCH",
                    report_type="hotel_statistics",
                    business_date=business_date,
                    metric_label=label,
                    period_label=period,
                    is_prior_year=period in ("LY_PTD", "LY_YTD"),
                    value=Decimal(w.text.replace(",", "")),
                )
            )
    return out
