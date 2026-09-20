"""All Payments: payment-type totals for the range, staged; the total row is the footing check."""

from datetime import date
from decimal import Decimal

from usali.adaptors.hotelkey_xlsx import (
    amount,
    expect_title,
    require_columns,
    section,
    split_report,
)
from usali.adaptors.pdf import Word
from usali.schemas import StagedRecord

_COLUMNS = ("Payment Type", "Amount")


def parse_all_payments(
    words: list[Word], *, property_id: str, business_date: date
) -> list[StagedRecord]:
    report = split_report(words)
    expect_title(report, "All Payments")
    block = section(report, "All Payments - Payment Type")
    require_columns(block, _COLUMNS)
    out = [
        StagedRecord(
            property_id=property_id,
            pms_source="HOTELKEY",
            report_type="all_payments",
            business_date=business_date,
            pms_trx_code=row["Payment Type"],
            pms_trx_desc=row["Payment Type"],
            raw_amount=amount(block, row, "Amount"),
            section="All Payments",
        )
        for row in block.rows
    ]
    total = sum((r.raw_amount for r in out), Decimal("0"))
    if block.total is None:
        raise ValueError("HotelKey All Payments has no total row")
    total_row = amount(block, block.total, "Amount")
    if total_row != total:
        raise ValueError(f"HotelKey All Payments does not foot: rows sum to {total}, "
                         f"total row says {total_row}")
    return out
