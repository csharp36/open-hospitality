import logging
import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_amount, parse_autoclerk_date
from usali.schemas import SegmentRecord

_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
# Rate codes are short uppercase alnum tokens that always carry at least one letter
# (distinguishes them from purely numeric tokens like a street address "2650").
_CODE_RE = re.compile(r"^(?=.*[A-Z])[A-Z0-9]{2,4}$")
_CODE_MAX_X0 = 40.0  # rate codes hug the left edge
_PLAIN_NUMBER_RE = re.compile(r"^\d[\d,]*(\.\d+)?$")
_DOLLAR_AMOUNT_RE = re.compile(r"^\$[\d,]+\.\d{2}$")

# Order values appear in once "$"+number pairs are merged and "%" tokens are dropped.
_DATA_MEASURES = ["ROOMS", "ADR", "ROOM_REVENUE", "NONROOM_REVENUE", "TOTAL_REVENUE"]

_LOG = logging.getLogger(__name__)


def extract_business_date(words: list[Word]) -> date:
    for w in words:
        if _DATE_RE.match(w.text):
            return parse_autoclerk_date(w.text)
    raise ValueError("no Autoclerk business date (M/D/YYYY) found in report")


def _row_values(cells: list[Word], where: str) -> list[Decimal]:
    # Walk a row's cells left-to-right, merging a lone "$" token with the numeric token
    # that follows it, parsing an already-merged "$1,234.56" token directly, and dropping
    # "%"-suffixed tokens entirely (percents are derivable from the kept values).
    # A refusal names the row by its position and counts its tokens; the tokens
    # themselves stay out of the message, which reaches IngestBatch.message, the
    # filed error record and the upload's HTTP response (ingestion._safe_message).
    values: list[Decimal] = []
    i = 0
    n = len(cells)
    while i < n:
        text = cells[i].text
        if text == "$":
            if i + 1 >= n:
                raise ValueError(
                    f"{where}: a '$' token is the last of its {n} tokens, "
                    f"with no amount after it"
                )
            values.append(parse_amount(text + cells[i + 1].text))
            i += 2
            continue
        if _DOLLAR_AMOUNT_RE.match(text):
            values.append(parse_amount(text))
            i += 1
            continue
        if text.endswith("%"):
            i += 1
            continue
        if _PLAIN_NUMBER_RE.match(text):
            values.append(Decimal(text.replace(",", "")))
            i += 1
            continue
        i += 1
    return values


def parse_rate_plan(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[SegmentRecord]:
    records: list[SegmentRecord] = []
    totals_values: list[Decimal] | None = None

    for row_number, row in enumerate(cluster_rows(words, y_tol), start=1):
        where = f"Autoclerk rate plan row {row_number}"
        cells = sorted(row, key=lambda w: w.x0)
        if not cells:
            continue
        first = cells[0]
        is_totals = any(w.text == "TOTALS:" for w in cells)

        if is_totals:
            value_cells = [w for w in cells if w.text != "TOTALS:"]
            values = _row_values(value_cells, where)
            if len(values) != 5:
                raise ValueError(
                    f"{where}: TOTALS row expected 5 values, found {len(values)} "
                    f"in {len(cells)} tokens"
                )
            totals_values = values
            for measure, value in zip(_DATA_MEASURES, values, strict=True):
                records.append(
                    SegmentRecord(
                        property_id=property_id,
                        pms_source="AUTOCLERK",
                        report_type="rate_plan",
                        business_date=business_date,
                        segment_code="TOTAL",
                        segment_desc=None,
                        measure=measure,
                        period_label="DAY",
                        value=value,
                    )
                )
            continue

        if first.x0 >= _CODE_MAX_X0 or not _CODE_RE.match(first.text):
            continue

        code = first.text
        values = _row_values(cells[1:], where)
        if len(values) != 5:
            # An incomplete data row (e.g. a page break mid-row) is not itself proof of
            # corruption -- the reconciliation against the TOTALS row below is what makes
            # a bad parse loud, so skip rather than fail fast on this row alone.
            # DOCUMENTED BLIND SPOT: reconciliation guards only ROOMS and ROOM_REVENUE,
            # so a corrupted row whose rooms AND room revenue are both exactly zero
            # would vanish without tripping it (no such row exists in real reports —
            # even FX2's loss changes the ROOMS sum). The warning below is the audit
            # trail for that narrow case; do not "fix" this by making skips raise.
            _LOG.warning(
                "skipping malformed rate-plan row %r: expected 5 values, got %d (%s)",
                code,
                len(values),
                [w.text for w in cells],
            )
            continue
        for measure, value in zip(_DATA_MEASURES, values, strict=True):
            records.append(
                SegmentRecord(
                    property_id=property_id,
                    pms_source="AUTOCLERK",
                    report_type="rate_plan",
                    business_date=business_date,
                    segment_code=code,
                    segment_desc=None,
                    measure=measure,
                    period_label="DAY",
                    value=value,
                )
            )

    # Reconciliation must be LOUD, never a silent no-op: a missing or mismatched TOTALS
    # row means we could not verify the parse, so refuse rather than return unverified data.
    if totals_values is None:
        raise ValueError("no TOTALS row found in report; cannot verify the parse")

    total_rooms = totals_values[0]
    total_room_revenue = totals_values[2]
    sum_rooms = sum(
        (r.value for r in records if r.segment_code != "TOTAL" and r.measure == "ROOMS"),
        Decimal("0"),
    )
    sum_room_revenue = sum(
        (r.value for r in records if r.segment_code != "TOTAL" and r.measure == "ROOM_REVENUE"),
        Decimal("0"),
    )
    if sum_rooms != total_rooms:
        raise ValueError(f"parsed ROOMS sum {sum_rooms} != report TOTALS ROOMS {total_rooms}")
    if sum_room_revenue != total_room_revenue:
        raise ValueError(
            f"parsed ROOM_REVENUE sum {sum_room_revenue} != "
            f"report TOTALS ROOM_REVENUE {total_room_revenue}"
        )
    return records
