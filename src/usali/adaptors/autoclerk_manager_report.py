import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_autoclerk_date
from usali.schemas import StatisticRecord

# Business date is printed zero-padded or not ("07/07/2026" / "7/7/2026"); Forecast rows
# use MM/DD/YY (2-digit year) and never match this. First match in reading order wins.
_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_COLUMN_LABELS = {"Today", "Yesterday", "Tomorrow", "MTD", "YTD"}
_SECTION_MAX_X0 = 45.0  # section headers hug the left margin


def extract_business_date(words: list[Word]) -> date:
    for w in words:
        if _DATE_RE.match(w.text):
            return parse_autoclerk_date(w.text)
    raise ValueError("no Autoclerk business date (M/D/YYYY) found in report")


def _to_decimal(text: str) -> Decimal | None:
    cleaned = text.replace("$", "").replace(",", "").replace("%", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if not cleaned or not re.match(r"^-?\d+(\.\d+)?$", cleaned):
        return None
    value = Decimal(cleaned)
    return -value if negative else value


def parse_manager_report(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StatisticRecord]:
    records: list[StatisticRecord] = []
    section: str | None = None
    anchors: list[tuple[str, float]] = []  # (column label, x0) for the current section,
    # left-to-right (cells are pre-sorted by x0 before this list is built)

    for row in cluster_rows(words, y_tol):
        cells = sorted(row, key=lambda w: w.x0)
        labels_in_row = [(w.text, w.x0) for w in cells if w.text in _COLUMN_LABELS]
        if cells[0].x0 < _SECTION_MAX_X0:
            if labels_in_row:
                # A section header row: name at the left margin + its column labels.
                section = " ".join(
                    w.text for w in cells if w.text not in _COLUMN_LABELS
                ).strip()
                anchors = labels_in_row
            else:
                # Far-left row WITHOUT recognized column labels (Ledger summary block,
                # Forecast header/sub-header, Forecast data rows which start with a date
                # at the left margin, page furniture): the previous section's anchors no
                # longer apply here or beyond — reset so following rows are not
                # mis-captured under a stale section.
                section = None
                anchors = []
            continue
        if section is None or not anchors:
            continue
        boundary = anchors[0][1] - 50
        name = " ".join(w.text for w in cells if w.x0 < boundary and w.text != "%").strip()
        if not name:
            continue
        # Match value cells to columns by left-to-right POSITION, not nearest x0 distance:
        # right-aligned numbers of differing width (e.g. "$36,246.81" vs "$1,019,221.27")
        # shift left under a fixed right edge, so absolute x0 proximity to a column anchor
        # is unreliable and can silently swap adjacent columns. A placeholder token like
        # "n/a" still occupies its column's position so the columns after it stay aligned;
        # it is simply dropped once `_to_decimal` returns None for it.
        # (Contrast with opera_manager_flash, which uses nearest-anchor because its report
        # has sparse rows WITHOUT placeholders. Caveat: zip truncates when a captured row
        # has fewer values than anchors and no placeholder — observed only on the never-
        # promoted "City Ledger - Total Running Balance:" noise row. Add a parity guard
        # before ever promoting City Ledger metrics.)
        value_cells = [w for w in cells if w.x0 >= boundary and w.text != "%"]
        for (column, _anchor_x0), w in zip(anchors, value_cells):
            value = _to_decimal(w.text)
            if value is None:  # 'n/a' or other non-numeric placeholder — skipped, not zeroed
                continue
            records.append(
                StatisticRecord(
                    property_id=property_id,
                    pms_source="AUTOCLERK",
                    report_type="manager_report",
                    business_date=business_date,
                    metric_label=f"{section} - {name}",
                    period_label=column,
                    is_prior_year=False,
                    value=value,
                )
            )
    return records
