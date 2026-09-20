"""The shape every HotelKey spreadsheet export shares, recovered from the Word grid.

Rows 1-2: property name and code in column A; the run stamps sit in the
right-most used column of rows 1-4. The title is the first single-cell row in
column A after row 4. Then sections::

    <label>                                (one cell, column B)
    <header ...>                           (cells from column B)
    <ordinal> <cells under the header>     detail row
    <ordinal>            ... <amount>      subtotal row (column B empty)
                         <totals>          total row (columns A and B empty)
    END OF REPORT                          (column A)

Classification is by which columns are populated, never by the ordinal alone:
the settlement export prints an ordinal on its subtotal rows too
(tests/adaptors/test_hotelkey_xlsx.py::test_split_report_recovers_the_shared_shape).
A missing END OF REPORT is a truncated export and is refused
(test_split_report_refuses_a_truncated_export).
"""

from dataclasses import dataclass, field

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
        if list(cells) == [2]:
            current = HkSection(label=cells[2])
            sections.append(current)
            continue
        if current is None:
            continue
        if not current.header:
            if 1 in cells or 2 not in cells:
                raise ValueError(f"HotelKey section {current.label!r} has no header row")
            current.header_cols = sorted(c for c in cells if c >= 2)
            current.header = [cells[c] for c in current.header_cols]
            continue
        named = {name: cells.get(c, "") for name, c in zip(current.header, current.header_cols)}
        if _is_ordinal(cells.get(1)):
            if 2 in cells:
                current.rows.append(named)
            else:
                current.subtotals.append([cells[c] for c in sorted(cells) if c != 1])
            continue
        if 1 not in cells and 2 not in cells:
            current.total = named
    if not terminated:
        raise ValueError("HotelKey export is truncated: no END OF REPORT row")
    if title is None:
        raise ValueError("HotelKey export has no title row")
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
