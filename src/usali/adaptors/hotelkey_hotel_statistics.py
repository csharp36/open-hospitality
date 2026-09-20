"""Parse HotelKey's "Hotel Statistics" PDF: statistics AND day-grain financial rows.

One report, two record kinds (design D-OH22.3). Every section is a heading row,
a header row carrying the five period columns, then label rows with values
centred under those column tokens::

    Room Statistics
    Description        Actual Today   M-T-D   LY-M-T-D    Y-T-D   LY-T-D
    Total Rooms                  60     780          0    3,540        0
    Rooms Available To           60     777          0    3,526        0
    Sell

A label wraps onto following rows that carry no values ("Sell" above); those
rows are joined to the label, which is what keeps the four occupancy variants
apart (tests/adaptors/test_hotelkey_hotel_statistics.py::
test_wrapped_labels_are_joined_so_the_four_occupancy_variants_stay_distinct).
A heading is a valueless row directly above a header row (valueless because a
sub-section's Totals row is also directly above the next sub-section's header:
test_financial_section_totals_are_statistics_and_their_lines_are_not); the
header row's prefix names a sub-section when it is not "Description" ("Room
Revenue", "Misc Revenue");
rows that repeat the report title are the running footer and are skipped
(test_page_furniture_never_becomes_a_metric). A page break may repeat the
header row with no heading; the previous heading carries
(test_rows_after_the_page_three_bare_header_are_parsed).

Columns are located from the header row's five period tokens by x0 (all five
present, ascending) and values are assigned to the NEAREST column, because
sparse rows print no placeholder (test_sparse_rows_assign_by_nearest_column).
Values are centred under the column tokens, so a wide one's x0 lands left of
the first period anchor, which is why the label/value boundary is the
"Actual" token's x0 less a margin and why assignment is by nearest anchor.
Periods are emitted canonical -- DAY / MTD / YTD with ``is_prior_year`` for
the two LY columns -- the labels ``stats_promote._CANONICAL_PERIODS`` maps
(tests/test_stats_promote.py::test_promotes_curated_metrics_and_canonical_periods).

The statistics side is lenient about non-numeric tokens in the value zone --
a ``(6)`` or a stray ``,`` is skipped
(test_stray_commas_in_the_value_zone_are_not_numbers) -- while the financial
side refuses a line missing a column.

``parse_financial_rows`` returns the Revenue Statistics, Taxes and Payments
lines (never the "Totals" rows) as StagedRecords at the Actual Today value, and
refuses the file if any of those sections, or any sub-section that appeared
under them, does not foot to its Totals row
(test_a_financial_section_that_does_not_foot_is_refused,
test_a_report_without_misc_revenue_still_foots). D-OH22.6 stages
these rows and does not promote them; the section Totals are emitted as
statistics instead, labelled "<section> / Totals".
"""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.schemas import StagedRecord, StatisticRecord

# Grouped ("1,120,000.00") or plain ("2480") digits; a stray "," or "2,,200" is not a number.
_NUM_RE = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")
_PERIOD_TOKENS = ("Today", "M-T-D", "LY-M-T-D", "Y-T-D", "LY-T-D")
# column index -> (canonical period, is_prior_year)
_PERIODS: list[tuple[str, bool]] = [("DAY", False), ("MTD", False), ("MTD", True),
                                    ("YTD", False), ("YTD", True)]
_REPORT_TITLE = "Hotel Statistics"
_FINANCIAL_HEADINGS = frozenset({"Revenue Statistics", "Taxes", "Payments"})
_TOTALS = "Totals"
_LABEL_MARGIN = 10.0


@dataclass(frozen=True)
class _Header:
    anchors: list[float]   # x0 of the five period tokens, ascending
    label_limit: float     # words left of this are label text
    prefix: str            # "Description", or a sub-section name


@dataclass
class _Block:
    heading: str
    section: str          # the sub-section (header prefix) or the heading
    label_parts: list[str]
    values: dict[int, Decimal] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return " ".join(self.label_parts)


def _header(cells: list[Word]) -> _Header | None:
    by_text = {w.text: w for w in cells}
    if not all(tok in by_text for tok in _PERIOD_TOKENS):
        return None
    anchors = [by_text[tok].x0 for tok in _PERIOD_TOKENS]
    if not all(a < b for a, b in zip(anchors, anchors[1:])):
        return None
    actual = by_text.get("Actual")
    label_limit = (actual.x0 if actual is not None else anchors[0]) - _LABEL_MARGIN
    prefix = " ".join(w.text for w in cells if w.x0 < label_limit).strip()
    return _Header(anchors=anchors, label_limit=label_limit, prefix=prefix)


def _values(cells: list[Word], label_limit: float) -> list[Word]:
    return [w for w in cells if w.x0 >= label_limit and _NUM_RE.match(w.text)]


def _nearest_column(x0: float, anchors: list[float]) -> int:
    return min(range(len(anchors)), key=lambda i: abs(anchors[i] - x0))


def _blocks(words: list[Word], y_tol: float) -> tuple[list[_Block], list[tuple[str, str]]]:
    """Blocks in page order, plus every (heading, sub-section) that had a header row."""
    rows = [sorted(r, key=lambda w: w.x0) for r in cluster_rows(words, y_tol)]
    headers = [_header(cells) for cells in rows]
    blocks: list[_Block] = []
    subsections: list[tuple[str, str]] = []
    header: _Header | None = None
    heading = ""     # last heading row seen ("Room Statistics", "Revenue Statistics", ...)
    section = ""     # heading, or the header row's prefix when it names a sub-section
    header_since_totals = False
    for i, cells in enumerate(rows):
        found = headers[i]
        if found is not None:
            header = found
            header_since_totals = True
            prefix = header.prefix
            if prefix and prefix != "Description":
                section = prefix
                if (heading, section) not in subsections:
                    subsections.append((heading, section))
            else:
                section = heading
            continue
        text = " ".join(w.text for w in cells)
        if text == _REPORT_TITLE:
            continue                      # the page-1 title and the running footer
        below = headers[i + 1] if i + 1 < len(rows) else None
        if below is not None and not _values(cells, below.label_limit):
            # A valueless row directly above a header row is that header's
            # section heading. "Valueless" matters: a sub-section's Totals row
            # also sits directly above the next sub-section's header.
            heading = text
            continue
        if header is None:
            continue                      # the page-1 header block
        label = " ".join(w.text for w in cells if w.x0 < header.label_limit).strip()
        values = _values(cells, header.label_limit)
        if not label:
            continue                      # "Page1 / 3": no label, one stray page count
        if not values:
            if blocks:
                blocks[-1].label_parts.append(label)   # a wrapped label line
            continue
        block = _Block(heading=heading, section=section, label_parts=[label])
        for w in values:
            column = _nearest_column(w.x0, header.anchors)
            if column in block.values:
                # Nearest-column assignment must never overwrite a value
                # (test_two_values_under_one_column_are_refused).
                raise ValueError(
                    f"HotelKey Hotel Statistics row {label!r} has two values under one column"
                )
            block.values[column] = Decimal(w.text.replace(",", ""))
        if label == _TOTALS:
            # Totals directly after Totals with no header row between them is
            # the enclosing section's grand total. A header between them means
            # a sub-section with no lines, whose own Totals this is
            # (test_an_empty_sub_section_keeps_its_own_totals_row).
            if (blocks and blocks[-1].label == _TOTALS and blocks[-1].heading == heading
                    and not header_since_totals):
                block.section = heading
            header_since_totals = False
        blocks.append(block)
    if header is None:
        raise ValueError(
            "HotelKey Hotel Statistics column header not found: expected a row carrying "
            "'Today', 'M-T-D', 'LY-M-T-D', 'Y-T-D' and 'LY-T-D' in ascending column order"
        )
    return blocks, subsections


def parse_hotel_statistics(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StatisticRecord]:
    out: list[StatisticRecord] = []
    blocks, _ = _blocks(words, y_tol)
    for block in blocks:
        financial = block.heading in _FINANCIAL_HEADINGS
        if financial and block.label != _TOTALS:
            continue  # a financial line: parse_financial_rows owns it
        label = f"{block.section} / {_TOTALS}" if financial else block.label
        for column, value in sorted(block.values.items()):
            period, prior = _PERIODS[column]
            out.append(
                StatisticRecord(
                    property_id=property_id,
                    pms_source="HOTELKEY",
                    report_type="hotel_statistics",
                    business_date=business_date,
                    metric_label=label,
                    period_label=period,
                    is_prior_year=prior,
                    value=value,
                )
            )
    return out


def parse_financial_rows(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StagedRecord]:
    out: list[StagedRecord] = []
    sums: dict[str, list[Decimal]] = {}       # section -> per-column running sums
    totals: dict[str, list[Decimal]] = {}     # section -> the Totals row's five values
    zeros = [Decimal("0")] * len(_PERIODS)
    blocks, subsections = _blocks(words, y_tol)
    for block in blocks:
        if block.heading not in _FINANCIAL_HEADINGS:
            continue
        if len(block.values) != len(_PERIODS):
            raise ValueError(
                f"HotelKey {block.heading} line {block.label!r} has {len(block.values)} of "
                f"{len(_PERIODS)} values; a financial line must carry every column"
            )
        column_values = [block.values[c] for c in range(len(_PERIODS))]
        if block.label == _TOTALS:
            totals[block.section] = column_values
            continue
        acc = sums.setdefault(block.section, list(zeros))
        for c, v in enumerate(column_values):
            acc[c] += v
        out.append(
            StagedRecord(
                property_id=property_id,
                pms_source="HOTELKEY",
                report_type="hotel_statistics",
                business_date=business_date,
                pms_trx_code=block.label,
                pms_trx_desc=block.label,
                raw_amount=block.values[0],   # Actual Today
                section=block.section,
            )
        )
    # Which sub-sections exist is the report's decision: every one that had a
    # header row must close with a Totals row, as must the three sections.
    financial_subsections = [(h, s) for h, s in subsections if h in _FINANCIAL_HEADINGS]
    for section in ("Revenue Statistics", "Taxes", "Payments", *(s for _, s in financial_subsections)):
        if section not in totals:
            raise ValueError(f"HotelKey Hotel Statistics has no Totals row for {section!r}")
    for section, expected in totals.items():
        subs = [s for h, s in financial_subsections if h == section]
        if subs:
            # Revenue Statistics has no lines of its own; its Totals row is the
            # grand total of the sub-section lines, so it foots against their sums.
            got = [sum(col, Decimal("0")) for col in zip(*(sums.get(s, zeros) for s in subs))]
        else:
            got = sums.get(section, zeros)
        if got != expected:
            raise ValueError(
                f"HotelKey {section} does not foot: lines sum to "
                f"{', '.join(str(v) for v in got)}, Totals row says "
                f"{', '.join(str(v) for v in expected)}"
            )
    return out
