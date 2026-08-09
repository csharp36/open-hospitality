import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.schemas import StatisticRecord

_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")
# Six value columns anchored by the header row "DAY MONTH YEAR DAY MONTH YEAR":
# the first three are the current year, the last three the prior year.
_COLUMN_PERIODS: list[tuple[str, bool]] = [
    ("DAY", False), ("MONTH", False), ("YEAR", False),
    ("DAY", True), ("MONTH", True), ("YEAR", True),
]


def _find_column_anchors(rows: list[list[Word]]) -> list[float]:
    for row in rows:
        texts = [w.text for w in sorted(row, key=lambda w: w.x0)]
        if texts == ["DAY", "MONTH", "YEAR", "DAY", "MONTH", "YEAR"]:
            return [w.x0 for w in sorted(row, key=lambda w: w.x0)]
    raise ValueError("Manager Flash column header row (DAY MONTH YEAR x2) not found")


def _nearest_column(x0: float, anchors: list[float]) -> int:
    # Column assignment strategy: NEAREST anchor by x0, deliberately different from the
    # Autoclerk manager-report parser's ordinal zip. This report has genuinely sparse
    # rows ("% Rooms Occupied for Tomorrow" fills only columns 1 and 4 with no
    # placeholder tokens), so pairing values with anchors by position would misalign;
    # the ~59-74pt anchor gaps comfortably absorb the ≤15pt left-drift of wide values.
    return min(range(len(anchors)), key=lambda i: abs(anchors[i] - x0))


def parse_manager_flash(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StatisticRecord]:
    rows = cluster_rows(words, y_tol)
    anchors = _find_column_anchors(rows)
    label_boundary = anchors[0] - 45  # labels sit far left; values right-align near anchors

    records: list[StatisticRecord] = []
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        label = " ".join(w.text for w in cells if w.x0 < label_boundary).strip()
        values = [w for w in cells if w.x0 >= label_boundary and _NUM_RE.match(w.text)]
        if not label or not values:
            continue
        for w in values:
            period, prior = _COLUMN_PERIODS[_nearest_column(w.x0, anchors)]
            records.append(
                StatisticRecord(
                    property_id=property_id,
                    pms_source="OPERA",
                    report_type="manager_flash",
                    business_date=business_date,
                    metric_label=label,
                    period_label=period,
                    is_prior_year=prior,
                    value=Decimal(w.text.replace(",", "")),
                )
            )
    return records
