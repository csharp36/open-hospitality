"""Settlement By Payment Type: one StagedRecord per settlement transaction.

Guest names, the card descriptor and the operator's username are parsed with
the row and never staged: pms_trx_desc carries category, transaction number,
folio and room only (tests/adaptors/test_hotelkey_xlsx.py::
test_settlement_rows_are_transaction_grain_without_guest_names). Each row keeps
its own Date; the export can span two days. ``business_date`` is accepted for
the pipeline's uniform handler signature and is not written to any row. The
Summary block is not staged -- it is the footing check: it must list exactly
the payment types the details carry, once each
(test_settlement_refuses_when_the_summary_omits_a_payment_type); per payment
type, amount and count must equal the details
(test_settlement_refuses_when_the_summary_disagrees_with_the_details,
test_settlement_refuses_when_a_summary_count_disagrees); and the grand totals
must agree (test_settlement_refuses_when_the_summary_total_disagrees).
"""

from datetime import date
from decimal import Decimal

from usali.adaptors.hotelkey_xlsx import (
    amount,
    count,
    expect_title,
    require_columns,
    section,
    split_report,
)
from usali.adaptors.pdf import Word
from usali.schemas import StagedRecord

_DETAIL_COLUMNS = (
    "Account Category", "Date", "Transaction Number", "Folio Number", "Room Number",
    "Payment Type", "Amount",
)
_SUMMARY_COLUMNS = ("Payment Type", "Amount", "Count")


def parse_settlement(
    words: list[Word], *, property_id: str, business_date: date
) -> list[StagedRecord]:
    report = split_report(words)
    expect_title(report, "Settlement By Payment Type")
    details = section(report, "Details")
    summary = section(report, "Summary")
    require_columns(details, _DETAIL_COLUMNS)
    require_columns(summary, _SUMMARY_COLUMNS)
    out: list[StagedRecord] = []
    by_type: dict[str, list[Decimal]] = {}
    for row in details.rows:
        value = amount(details, row, "Amount")
        ptype = row["Payment Type"]
        by_type.setdefault(ptype, []).append(value)
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
                raw_amount=value,
                section="Details",
            )
        )
    grand = sum((r.raw_amount for r in out), Decimal("0"))
    if details.total is None:
        raise ValueError("HotelKey settlement Details have no total row")
    details_total = amount(details, details.total, "Amount")
    if details_total != grand:
        raise ValueError(f"HotelKey settlement Details do not foot: rows sum to {grand}, "
                         f"total row says {details_total}")
    listed = [srow["Payment Type"] for srow in summary.rows]
    if len(set(listed)) != len(listed) or set(listed) != set(by_type):
        raise ValueError(
            f"HotelKey settlement Summary lists {listed} but Details carry {sorted(by_type)}"
        )
    for srow in summary.rows:
        ptype = srow["Payment Type"]
        amounts = by_type[ptype]
        detail_total = sum(amounts, Decimal("0"))
        if detail_total != amount(summary, srow, "Amount") or len(amounts) != count(summary, srow, "Count"):
            raise ValueError(
                f"HotelKey settlement Summary disagrees with Details for {ptype}: "
                f"summary {srow['Amount']} x{srow['Count']}, details {detail_total} x{len(amounts)}"
            )
    if summary.total is None:
        raise ValueError("HotelKey settlement Summary has no total row")
    if (
        amount(summary, summary.total, "Amount") != grand
        or count(summary, summary.total, "Count") != len(out)
    ):
        raise ValueError(
            f"HotelKey settlement Summary total disagrees with Details: "
            f"summary {summary.total['Amount']} x{summary.total['Count']}, "
            f"details {grand} x{len(out)}"
        )
    return out
