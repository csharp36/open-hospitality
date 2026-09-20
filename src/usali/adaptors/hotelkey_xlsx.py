"""The shape every HotelKey spreadsheet export shares, recovered from the Word grid.

Rows 1-2: property name and code in column A; the run stamps sit in the
right-most used column of rows 1-4 and are ignored here. The title is the
first single-cell column-A row after the property code row. Then sections::

    <label>                                (one cell, column B)
    <header ...>                           (cells from column B)
    <ordinal> <cells under the header>     detail row
    <ordinal>            ... <amount>      subtotal row (column B empty)
                         <totals>          total row (columns A and B empty)
    END OF REPORT                          (column A)

Classification is by which columns are populated, never by the ordinal alone:
the settlement export prints an ordinal on its subtotal rows too
(tests/adaptors/test_hotelkey_xlsx.py::test_split_report_recovers_the_shared_shape).
The classification is closed: a row inside a section that is none of the three
is refused (test_a_row_whose_column_a_is_not_an_ordinal_is_refused,
test_a_row_with_cells_but_no_ordinal_is_refused), as is a second total row
(test_a_second_total_row_is_refused), a label with no header row after it
(test_two_consecutive_label_rows_are_refused,
test_a_label_directly_before_end_of_report_is_refused) and a missing
END OF REPORT (test_split_report_refuses_a_truncated_export).

``amount`` is the one way a parser turns a cell into a Decimal: ``Decimal``
itself raises ``InvalidOperation`` on blank or non-numeric text, which a
caller catching ``ValueError`` never sees (the same reasoning as
``rates.CorruptRateError``), so it is turned into a ``ValueError`` naming the
section, row and column here (test_a_blank_amount_cell_is_refused,
test_a_non_numeric_amount_is_refused); ``count`` is its integer twin for the
settlement Summary (test_a_blank_count_cell_is_refused). ``require_columns``
turns a renamed header into a ``ValueError`` rather than a ``KeyError`` deep
in a parser (test_a_renamed_header_column_is_refused).

A refusal that describes a row echoes only its populated column letters and
its column-A text (an ordinal or END OF REPORT), never the other cells: on the
settlement export those hold guest names, card descriptors and usernames
(test_a_row_whose_column_a_is_not_an_ordinal_is_refused pins the exclusion).
``amount`` and ``count`` echo the offending cell and the row's first header
column only; in the four HotelKey exports read 2026-09-20 that column is a
category, a payment type or an organization, not a person.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from usali.adaptors.pdf import Word, cluster_rows
from usali.adaptors.xlsx import XLSX_COL_STEP

_END = "END OF REPORT"


@dataclass
class HkSection:
    label: str
    header: list[str] = field(default_factory=list)
    header_cols: list[int] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    subtotals: list[list[str]] = field(default_factory=list)
    total: dict[str, str] | None = None


@dataclass(frozen=True)
class HkReport:
    property_name: str
    property_code: str
    title: str
    sections: list[HkSection]


def _grid(words: list[Word]) -> list[dict[int, str]]:
    rows: list[dict[int, str]] = []
    for row in cluster_rows(words):
        rows.append({int(round(w.x0 / XLSX_COL_STEP)): w.text for w in row})
    return rows


def _letter(column: int) -> str:
    out = ""
    while column:
        column, rem = divmod(column - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _shape(cells: dict[int, str]) -> str:
    others = ", ".join(_letter(c) for c in sorted(cells) if c != 1)
    if 1 not in cells:
        return f"cells in {others}"
    head = f"column A {cells[1]!r}"
    return f"{head} and cells in {others}" if others else f"{head} only"


def _is_ordinal(text: str | None) -> bool:
    return text is not None and text.isdigit()


def split_report(words: list[Word]) -> HkReport:
    grid = _grid(words)
    if len(grid) < 3 or 1 not in grid[0] or 1 not in grid[1]:
        raise ValueError("HotelKey export header not found: property name and code in column A")
    property_name, property_code = grid[0][1], grid[1][1]
    title: str | None = None
    sections: list[HkSection] = []
    current: HkSection | None = None
    terminated = False
    for cells in grid[2:]:
        if cells.get(1) == _END:
            terminated = True
            break
        if title is None:
            if list(cells) == [1]:
                title = cells[1]
            continue
        if current is not None and not current.header:
            if list(cells) == [2]:
                raise ValueError(
                    f"HotelKey section {current.label!r} has no header row: "
                    f"the next row is the label {cells[2]!r}"
                )
            if 1 in cells or 2 not in cells:
                raise ValueError(
                    f"HotelKey section {current.label!r}: expected a header row from column B, "
                    f"found {_shape(cells)}"
                )
            current.header_cols = sorted(c for c in cells if c >= 2)
            current.header = [cells[c] for c in current.header_cols]
            continue
        if list(cells) == [2]:
            current = HkSection(label=cells[2])
            sections.append(current)
            continue
        if current is None:
            continue
        named = {name: cells.get(c, "") for name, c in zip(current.header, current.header_cols)}
        if _is_ordinal(cells.get(1)):
            if 2 in cells:
                current.rows.append(named)
            else:
                current.subtotals.append([cells[c] for c in sorted(cells) if c != 1])
        elif 1 not in cells and 2 not in cells:
            if current.total is not None:
                raise ValueError(
                    f"HotelKey section {current.label!r} has two total rows; "
                    f"the second has {_shape(cells)}"
                )
            current.total = named
        else:
            raise ValueError(
                f"HotelKey section {current.label!r}: row with {_shape(cells)} is not a detail, "
                f"subtotal or total row"
            )
    if not terminated:
        raise ValueError("HotelKey export is truncated: no END OF REPORT row")
    if title is None:
        raise ValueError("HotelKey export has no title row")
    if current is not None and not current.header:
        raise ValueError(
            f"HotelKey section {current.label!r} has no header row: END OF REPORT follows the label"
        )
    return HkReport(property_name=property_name, property_code=property_code, title=title,
                    sections=sections)


def section(report: HkReport, label: str) -> HkSection:
    for s in report.sections:
        if s.label == label:
            return s
    raise ValueError(f"HotelKey {report.title!r} has no {label!r} section")


def expect_title(report: HkReport, title: str) -> None:
    if report.title != title:
        raise ValueError(f"expected a HotelKey {title!r} export, found {report.title!r}")


def require_columns(section: HkSection, names: tuple[str, ...]) -> None:
    missing = [n for n in names if n not in section.header]
    if missing:
        raise ValueError(f"HotelKey {section.label!r} header lacks {missing}; found {section.header}")


def _refusal(section: HkSection, row: dict[str, str], column: str, what: str) -> ValueError:
    return ValueError(
        f"HotelKey {section.label!r} row {row.get(section.header[0], '?')!r}: "
        f"column {column!r} is not {what}: {row.get(column, '')!r}"
    )


def amount(section: HkSection, row: dict[str, str], column: str) -> Decimal:
    text = row.get(column, "")
    value: Decimal | None
    try:
        value = Decimal(text) if text else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite():
        raise _refusal(section, row, column, "an amount")
    return value


def count(section: HkSection, row: dict[str, str], column: str) -> int:
    text = row.get(column, "")
    if not text.isdigit():
        raise _refusal(section, row, column, "a count")
    return int(text)
