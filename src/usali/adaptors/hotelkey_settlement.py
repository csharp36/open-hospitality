"""Settlement By Payment Type: one StagedRecord per settlement transaction.

Guest names, the card descriptor and the operator's username are parsed with
the row and never staged: pms_trx_desc carries category, transaction number,
folio and room only (tests/adaptors/test_hotelkey_xlsx.py::
test_settlement_rows_are_transaction_grain_without_guest_names). Each row keeps
its own Date; the export can span two days. The Summary block is not staged --
it is the footing check: per payment type, amount and count must equal the
details, and the grand totals must agree
(test_settlement_refuses_when_the_summary_disagrees_with_the_details).
"""

from datetime import date
from decimal import Decimal

from usali.adaptors.hotelkey_xlsx import expect_title, section, split_report
from usali.adaptors.pdf import Word
from usali.schemas import StagedRecord


def parse_settlement(
    words: list[Word], *, property_id: str, business_date: date
) -> list[StagedRecord]:
    report = split_report(words)
    expect_title(report, "Settlement By Payment Type")
    details = section(report, "Details")
    summary = section(report, "Summary")
    out: list[StagedRecord] = []
    by_type: dict[str, list[Decimal]] = {}
    for row in details.rows:
        amount = Decimal(row["Amount"])
        ptype = row["Payment Type"]
        by_type.setdefault(ptype, []).append(amount)
        out.append(
            StagedRecord(
                property_id=property_id,
                pms_source="HOTELKEY",
                report_type="settlement",
                business_date=date.fromisoformat(row["Date"]),
                pms_trx_code=ptype,
                pms_trx_desc=(
                    f"{row['Account Category']} txn {row['Transaction Number']} "
                    f"folio {row['Folio Number']} room {row['Room Number']}"
                ),
                raw_amount=amount,
                section="Details",
            )
        )
    grand = sum((r.raw_amount for r in out), Decimal("0"))
    if details.total is None or Decimal(details.total["Amount"]) != grand:
        raise ValueError(f"HotelKey settlement Details do not foot: rows sum to {grand}, "
                         f"total row says {details.total and details.total['Amount']}")
    for srow in summary.rows:
        ptype = srow["Payment Type"]
        amounts = by_type.get(ptype, [])
        if sum(amounts, Decimal("0")) != Decimal(srow["Amount"]) or len(amounts) != int(srow["Count"]):
            raise ValueError(
                f"HotelKey settlement Summary disagrees with Details for {ptype}: "
                f"summary {srow['Amount']} x{srow['Count']}, details {sum(amounts, Decimal('0'))} x{len(amounts)}"
            )
    if summary.total is None or Decimal(summary.total["Amount"]) != grand or int(summary.total["Count"]) != len(out):
        raise ValueError("HotelKey settlement Summary total disagrees with Details")
    return out
