"""AR Invoice Aging: balance LedgerRecords, labelled "<section> / <row> / <column>".

Every section's total row is the same eight numbers (one AR book, four
groupings), which is the footing check
(tests/adaptors/test_hotelkey_xlsx.py::test_ar_aging_refuses_when_sections_disagree).
Staged: each section's total row, and the detail rows of the Transaction Type,
Company Name and Transaction Status sections. The per-invoice-date rows of the
Date section are not staged; they are the invoice ledger OH-29 will read from
the real export, not a balance.
"""

from datetime import date
from decimal import Decimal

from usali.adaptors.hotelkey_xlsx import HkSection, expect_title, split_report
from usali.adaptors.pdf import Word
from usali.schemas import LedgerRecord

_DATE_SECTION = "AR Aging Details - Date"


def _amounts(sec: HkSection, row: dict[str, str]) -> list[tuple[str, Decimal]]:
    return [(col, Decimal(row[col])) for col in sec.header[1:] if row.get(col, "") != ""]


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
            for col, amount in _amounts(sec, row):
                column_sums[col] = column_sums.get(col, Decimal("0")) + amount
                if sec.label != _DATE_SECTION:
                    emit(f"{sec.label} / {row[sec.header[0]]} / {col}", amount)
        for col, amount in totals:
            if column_sums.get(col, Decimal("0")) != amount:
                raise ValueError(
                    f"HotelKey AR aging {sec.label!r} column {col!r} does not foot: "
                    f"rows sum to {column_sums.get(col)}, total row says {amount}"
                )
            emit(f"{sec.label} / Total / {col}", amount)
    return out
