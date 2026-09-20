"""AR Invoice Aging: balance LedgerRecords, labelled "<section> / <row> / <column>".

Every section's total row is the same eight numbers (one AR book, four
groupings), which is the footing check
(tests/adaptors/test_hotelkey_xlsx.py::test_ar_aging_refuses_when_sections_disagree),
and within a section every amount column foots to its total
(test_ar_aging_refuses_when_a_section_column_does_not_foot). Each section's
header must name a label column followed by the eight aging columns
(``_AGING_COLUMNS``); a renamed column is refused before any row is read.
Staged: each section's total row, and the detail rows of the Transaction Type,
Company Name and Transaction Status sections. The per-invoice-date rows of the
Date section are not staged; they are invoice detail, not a balance, and are
deferred to the AR slice (design doc
docs/design/2026-09-08-oh22-hotelkey-design.md, D-OH22.5).
"""

from datetime import date
from decimal import Decimal

from usali.adaptors.hotelkey_xlsx import (
    HkSection,
    amount,
    expect_title,
    require_columns,
    split_report,
)
from usali.adaptors.pdf import Word
from usali.schemas import LedgerRecord

_DATE_SECTION = "AR Aging Details - Date"
_AGING_COLUMNS = (
    "Payment On Account", "Current", "31 to 60", "61 to 90", "91 to 120", "121 to 150",
    "Over 150", "Total",
)


def _amounts(sec: HkSection, row: dict[str, str]) -> list[tuple[str, Decimal]]:
    return [(col, amount(sec, row, col)) for col in sec.header[1:] if row.get(col, "") != ""]


def parse_ar_aging(
    words: list[Word], *, property_id: str, business_date: date
) -> list[LedgerRecord]:
    report = split_report(words)
    expect_title(report, "AR Invoice Aging")
    if not report.sections:
        raise ValueError("HotelKey AR Invoice Aging has no sections")
    out: list[LedgerRecord] = []

    def emit(label: str, amount: Decimal) -> None:
        out.append(
            LedgerRecord(
                property_id=property_id,
                pms_source="HOTELKEY",
                report_type="ar_aging",
                business_date=business_date,
                ledger_label=label,
                kind="balance",
                amount=amount,
            )
        )

    reference: list[tuple[str, Decimal]] | None = None
    for sec in report.sections:
        require_columns(sec, _AGING_COLUMNS)
        if sec.header[0] in _AGING_COLUMNS:
            raise ValueError(
                f"HotelKey {sec.label!r} header has no label column before the aging columns; "
                f"found {sec.header}"
            )
        if sec.total is None:
            raise ValueError(f"HotelKey AR aging section {sec.label!r} has no total row")
        totals = _amounts(sec, sec.total)
        if reference is None:
            reference = totals
        elif totals != reference:
            raise ValueError(
                f"HotelKey AR aging sections disagree: {sec.label!r} totals {totals} "
                f"but {report.sections[0].label!r} totals {reference}"
            )
        column_sums: dict[str, Decimal] = {}
        for row in sec.rows:
            for col, value in _amounts(sec, row):
                column_sums[col] = column_sums.get(col, Decimal("0")) + value
                if sec.label != _DATE_SECTION:
                    emit(f"{sec.label} / {row[sec.header[0]]} / {col}", value)
        for col, value in totals:
            if column_sums.get(col, Decimal("0")) != value:
                raise ValueError(
                    f"HotelKey AR aging {sec.label!r} column {col!r} does not foot: "
                    f"rows sum to {column_sums.get(col)}, total row says {value}"
                )
            emit(f"{sec.label} / Total / {col}", value)
    return out
