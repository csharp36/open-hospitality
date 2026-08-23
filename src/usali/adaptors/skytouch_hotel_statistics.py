"""Parse the SkyTouch "Hotel Statistics" report into per-period StatisticRecords.

Column anchors are located POSITIONALLY, not by matching a fixed token sequence,
because a real SkyTouch header is not a stable phrase. Each section repeats the
header, in two variants that differ by a "Current" prefix::

    Room Statistics       6/21/2026         PTD  Last Year PTD         YTD  Last YTD
    Performance Statistics 6/21/2026 Current PTD  Last Year PTD Current YTD  Last YTD

The five value columns sit at IDENTICAL x0 in both, under the LAST token of each
group. So the rule is: in a row carrying one M/D/YYYY date, exactly two ``PTD``
tokens and exactly two ``YTD`` tokens, the anchors are that date and each pair in
x0 order -- today, PTD, last-year PTD, YTD, last-year YTD. That is invariant to
the "Current"/"Last Year" wording around them.

(An earlier revision matched five synthetic single tokens emitted by the mock
generator and raised on any real file. Recalibrated against a real Standard Audit
Pack; the generator now emits the real header shape.)
"""

import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_skytouch_date
from usali.schemas import StatisticRecord

_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")
_PERIODS = ["ACTUAL", "PTD", "LY_PTD", "YTD", "LY_YTD"]
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def extract_business_date(words: list[Word]) -> date:
    for w in words[:80]:
        if _DATE_RE.match(w.text):
            return parse_skytouch_date(w.text)
    raise ValueError("no SkyTouch M/D/YYYY business date in Hotel Statistics header")


def _column_anchors(rows: list[list[Word]]) -> list[float]:
    """The five value-column x0s, taken from the repeated section header row.

    Identified by SHAPE rather than wording: one business date, two ``PTD`` and
    two ``YTD``. Values sit under the last token of each group, which is exactly
    what these five positions are.
    """
    for row in rows:
        cells = sorted(row, key=lambda w: w.x0)
        dates = [w for w in cells if _DATE_RE.match(w.text)]
        ptds = [w for w in cells if w.text == "PTD"]
        ytds = [w for w in cells if w.text == "YTD"]
        if len(dates) != 1 or len(ptds) != 2 or len(ytds) != 2:
            continue
        anchors = [dates[0].x0, ptds[0].x0, ptds[1].x0, ytds[0].x0, ytds[1].x0]
        # A header whose columns are not left-to-right is not a header we
        # understand; keep looking rather than mis-assign every period.
        if all(a < b for a, b in zip(anchors, anchors[1:])):
            return anchors
    raise ValueError(
        "SkyTouch Hotel Statistics column header not found: expected a row with "
        "one M/D/YYYY business date, two 'PTD' and two 'YTD' tokens in ascending "
        "column order"
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
