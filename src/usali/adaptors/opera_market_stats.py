import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.schemas import SegmentRecord

_CODE_RE = re.compile(r"^[A-Z0-9]{1,2}$")
_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?%?$")
_CODE_MAX_X0 = 20.0  # market codes hug the left edge
_MEASURES = ["ROOMS", "ROOM_REVENUE", "ADR", "OCC_PCT", "PERSONS"]
_PERIODS = ["DAY", "MONTH", "YEAR"]


def _find_measure_anchors(rows: list[list[Word]]) -> list[tuple[str, str, float]]:
    # The sub-header prints "Rooms Room Revenue ADR % Occ. Prs." once per period band.
    # Anchor each measure to the token its values right-align under: Rooms, Revenue
    # (second token of "Room Revenue"), ADR, % (first token of "% Occ."), Prs.
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        texts = [w.text for w in cells]
        if texts.count("Rooms") == 3 and texts.count("Revenue") == 3:
            anchors: list[tuple[str, str, float]] = []
            band = 0
            for w in cells:
                if w.text == "Rooms":
                    period = _PERIODS[band]
                    band += 1
                    anchors.append(("ROOMS", period, w.x0))
                elif w.text == "Revenue":
                    anchors.append(("ROOM_REVENUE", _PERIODS[band - 1], w.x0))
                elif w.text == "ADR":
                    anchors.append(("ADR", _PERIODS[band - 1], w.x0))
                elif w.text == "%":
                    anchors.append(("OCC_PCT", _PERIODS[band - 1], w.x0))
                elif w.text == "Prs.":
                    anchors.append(("PERSONS", _PERIODS[band - 1], w.x0))
            if len(anchors) == 15:
                return anchors
    raise ValueError("Market Code Statistics measure sub-header row not found")


def parse_market_stats(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[SegmentRecord]:
    rows = cluster_rows(words, y_tol)
    anchors = _find_measure_anchors(rows)
    value_region = min(x0 for _, _, x0 in anchors) - 15

    records: list[SegmentRecord] = []
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        first = cells[0]
        joined_head = " ".join(w.text for w in cells[:2])
        if joined_head == "Group Total":
            continue
        if joined_head == "Grand Total":
            code, desc_cells = "TOTAL", []
        elif first.x0 < _CODE_MAX_X0 and _CODE_RE.match(first.text):
            code = first.text
            desc_cells = [w for w in cells[1:] if w.x0 < value_region]
        else:
            continue
        desc = " ".join(w.text for w in desc_cells).strip() or None
        for w in cells:
            if w.x0 < value_region or not _NUM_RE.match(w.text):
                continue
            measure, period, _ = min(anchors, key=lambda a: abs(a[2] - w.x0))
            records.append(
                SegmentRecord(
                    property_id=property_id,
                    pms_source="OPERA",
                    report_type="market_stats",
                    business_date=business_date,
                    segment_code=code,
                    segment_desc=desc,
                    measure=measure,
                    period_label=period,
                    value=Decimal(w.text.replace(",", "").rstrip("%")),
                )
            )
    return records
