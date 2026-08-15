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
            idx = min(range(5), key=lambda i: abs(anchors[i] - w.x0))
            period = _PERIODS[idx]
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
