from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from usali.adaptors.hotelkey_all_payments import parse_all_payments
from usali.adaptors.hotelkey_ar_aging import parse_ar_aging
from usali.adaptors.hotelkey_settlement import parse_settlement
from usali.adaptors.hotelkey_xlsx import split_report
from usali.adaptors.pdf import Word
from usali.adaptors.reader import read_words
from usali.adaptors.xlsx import XLSX_COL_STEP, XLSX_ROW_STEP

FIX = Path("tests/fixtures/hotelkey")
BD = date(2026, 8, 13)


def _words(name: str) -> list[Word]:
    return read_words(FIX / name)


def _cell(cell: str) -> tuple[float, float]:
    """Sheet cell address (e.g. "N20") -> (x0, top) on the synthetic XLSX grid."""
    col, row = ord(cell[0]) - ord("A") + 1, int(cell[1:])
    return col * XLSX_COL_STEP, row * XLSX_ROW_STEP


def _at(cell: str, text: str) -> Word:
    x0, top = _cell(cell)
    return Word(text=text, x0=x0, top=top)


def _only(words: list[Word], cell: str) -> Word:
    x0, top = _cell(cell)
    hits = [w for w in words if w.x0 == x0 and w.top == top]
    assert len(hits) == 1, (cell, hits)
    return hits[0]


def _mutated(words: list[Word], cell: str, text: str) -> list[Word]:
    """Replace the one word at sheet cell ``cell`` with ``text``."""
    hit = _only(words, cell)
    return [Word(text=text, x0=w.x0, top=w.top) if w is hit else w for w in words]


def _without(words: list[Word], *cells: str) -> list[Word]:
    """Drop the one word at each of ``cells``; every cell must be populated."""
    gone = {id(_only(words, c)) for c in cells}
    return [w for w in words if id(w) not in gone]


def _grid(rows: list[dict[int, str]]) -> list[Word]:
    """Hand-built sheet: one dict per sheet row (1-based), column number -> text."""
    return [
        Word(text=t, x0=c * XLSX_COL_STEP, top=(i + 1) * XLSX_ROW_STEP)
        for i, cells in enumerate(rows)
        for c, t in cells.items()
    ]


def test_split_report_recovers_the_shared_shape():
    report = split_report(_words("Settlement By Payment Type.xlsx"))
    assert (report.property_name, report.property_code, report.title) == (
        "Lakeside Test Lodge", "HKTEST", "Settlement By Payment Type",
    )
    assert [s.label for s in report.sections] == ["Details", "Summary"]
    details, summary = report.sections
    assert details.header[:3] == ["Account Category", "Date", "Time"]
    assert len(details.rows) == 5 and details.rows[0]["Payment Type"] == "MASTER"
    assert details.rows[0]["Date"] == "2026-08-12"
    assert [r[0] for r in details.subtotals] == ["700", "200"]
    assert details.total is not None and details.total["Amount"] == "900"
    assert summary.total is not None
    assert (summary.total["Amount"], summary.total["Count"]) == ("900", "5")


def test_split_report_refuses_a_truncated_export():
    words = [w for w in _words("All Payments.xlsx") if w.text != "END OF REPORT"]
    with pytest.raises(ValueError, match="END OF REPORT"):
        split_report(words)


def test_settlement_rows_are_transaction_grain_without_guest_names():
    rows = parse_settlement(_words("Settlement By Payment Type.xlsx"), property_id="HKDEMO", business_date=BD)
    assert len(rows) == 5
    assert {r.pms_trx_code for r in rows} == {"MASTER", "VISA"}
    assert sum(r.raw_amount for r in rows) == Decimal("900.00")
    assert {r.business_date for r in rows} == {date(2026, 8, 12), date(2026, 8, 13)}
    assert all(r.report_type == "settlement" and r.section == "Details" for r in rows)
    first = rows[0]
    assert first.pms_trx_desc == "Reservation txn 10000001 folio 000101 room 201"
    joined = " ".join(r.pms_trx_desc or "" for r in rows)
    for pii in ("TESTGUEST", "ALPHA", "1111", "testusr01"):
        assert pii not in joined


def test_settlement_refuses_when_the_summary_disagrees_with_the_details():
    # C25 is the Summary MASTER amount (row 25: 1, MASTER, 700, 3); the Details
    # subtotal at N16 also reads 700 and is left alone.
    bad = _mutated(_words("Settlement By Payment Type.xlsx"), "C25", "701")
    with pytest.raises(ValueError, match="MASTER"):
        parse_settlement(bad, property_id="HKDEMO", business_date=BD)


def test_all_payments_rows_and_total():
    rows = parse_all_payments(_words("All Payments.xlsx"), property_id="HKDEMO", business_date=BD)
    assert {(r.pms_trx_code, r.raw_amount) for r in rows} == {("MASTER", Decimal("700")), ("VISA", Decimal("200"))}
    assert all(r.report_type == "all_payments" and r.section == "All Payments" for r in rows)


def test_ar_aging_emits_section_qualified_balances():
    recs = parse_ar_aging(_words("AR Invoice Aging.xlsx"), property_id="HKDEMO", business_date=BD)
    by = {r.ledger_label: r.amount for r in recs}
    assert by["AR Aging Details - Transaction Type / Total / Total"] == Decimal("3400")
    assert by["AR Aging Details - Transaction Type / Total / Over 150"] == Decimal("-100")
    assert by["AR Aging Details - Company Name / TEST CORP TRAVEL / Total"] == Decimal("3000")
    assert by["AR Aging Details - Transaction Status / CLOSED / Over 150"] == Decimal("-100")
    assert not any(label.startswith("AR Aging Details - Date / 2026") for label in by)
    assert "AR Aging Details - Date / Total / Total" in by
    assert all(r.kind == "balance" and r.report_type == "ar_aging" for r in recs)


def test_ar_aging_refuses_when_sections_disagree():
    # J31 is the Total cell of the Company Name section's total row (generator
    # row map: 27 label, 28 header, 29-30 rows, 31 total).
    bad = _mutated(_words("AR Invoice Aging.xlsx"), "J31", "3401")
    with pytest.raises(ValueError, match="Company Name"):
        parse_ar_aging(bad, property_id="HKDEMO", business_date=BD)


def test_settlement_refuses_when_details_do_not_foot_to_their_total():
    # N20 is the Details grand total (generator: row 20, column 14, unnumbered).
    bad = _mutated(_words("Settlement By Payment Type.xlsx"), "N20", "901")
    with pytest.raises(ValueError, match="Details do not foot"):
        parse_settlement(bad, property_id="HKDEMO", business_date=BD)


def test_settlement_refuses_when_a_summary_count_disagrees():
    # D25 is the Summary MASTER count (row 25: 1, MASTER, 700, 3); three details carry MASTER.
    bad = _mutated(_words("Settlement By Payment Type.xlsx"), "D25", "4")
    with pytest.raises(ValueError, match="MASTER.*x4"):
        parse_settlement(bad, property_id="HKDEMO", business_date=BD)


def test_settlement_refuses_when_the_summary_total_disagrees():
    # C27 is the Summary total amount (row 27: C = 900, D = 5).
    bad = _mutated(_words("Settlement By Payment Type.xlsx"), "C27", "901")
    with pytest.raises(ValueError, match="Summary total disagrees.*901.*900"):
        parse_settlement(bad, property_id="HKDEMO", business_date=BD)


def test_settlement_refuses_when_the_summary_omits_a_payment_type():
    # Row 26 is the Summary VISA row (2, VISA, 200, 2); the Details still carry VISA.
    bad = _without(_words("Settlement By Payment Type.xlsx"), "A26", "B26", "C26", "D26")
    with pytest.raises(ValueError, match="Summary lists"):
        parse_settlement(bad, property_id="HKDEMO", business_date=BD)


def test_all_payments_refuses_when_rows_do_not_foot():
    # C18 is the unnumbered total under rows 16-17 (700 + 200).
    bad = _mutated(_words("All Payments.xlsx"), "C18", "901")
    with pytest.raises(ValueError, match="All Payments does not foot"):
        parse_all_payments(bad, property_id="HKDEMO", business_date=BD)


def test_ar_aging_refuses_when_a_section_column_does_not_foot():
    # D29 is TEST CORP TRAVEL's Current cell in the Company Name section (row 29;
    # B = Company Name, C = Payment On Account, D = Current). The section's total
    # row still equals the other sections', so only the column footing can catch it.
    bad = _mutated(_words("AR Invoice Aging.xlsx"), "D29", "1001")
    with pytest.raises(ValueError, match="Company Name.*Current"):
        parse_ar_aging(bad, property_id="HKDEMO", business_date=BD)


def test_expect_title_refuses_the_wrong_report():
    with pytest.raises(ValueError, match="expected a HotelKey 'Settlement By Payment Type' export"):
        parse_settlement(_words("All Payments.xlsx"), property_id="HKDEMO", business_date=BD)


def test_a_missing_section_is_refused():
    # Rows 23-27 are the whole Summary block (label, header, two rows, total);
    # without it the Details section runs to END OF REPORT and the section
    # lookup is what refuses.
    without = _without(
        _words("Settlement By Payment Type.xlsx"),
        "B23", "B24", "C24", "D24", "A25", "B25", "C25", "D25", "A26", "B26", "C26", "D26", "C27", "D27",
    )
    with pytest.raises(ValueError, match="has no 'Summary' section"):
        parse_settlement(without, property_id="HKDEMO", business_date=BD)


def test_a_blank_amount_cell_is_refused():
    # C16 is the MASTER amount in All Payments (row 16: 1, MASTER, 700).
    bad = _without(_words("All Payments.xlsx"), "C16")
    with pytest.raises(ValueError, match="is not an amount"):
        parse_all_payments(bad, property_id="HKDEMO", business_date=BD)


@pytest.mark.parametrize("text", ["1,000", "NaN"])
def test_a_non_numeric_amount_is_refused(text: str):
    # N13 is the first Details row's Amount in the settlement export.
    bad = _mutated(_words("Settlement By Payment Type.xlsx"), "N13", text)
    with pytest.raises(ValueError, match="'Details' row 'Reservation': column 'Amount' is not an amount"):
        parse_settlement(bad, property_id="HKDEMO", business_date=BD)


def test_a_renamed_header_column_is_refused():
    # N12 is the Details header's "Amount".
    bad = _mutated(_words("Settlement By Payment Type.xlsx"), "N12", "Amt")
    with pytest.raises(ValueError, match=r"'Details' header lacks \['Amount'\]"):
        parse_settlement(bad, property_id="HKDEMO", business_date=BD)


def test_a_row_whose_column_a_is_not_an_ordinal_is_refused():
    # A13 is the first Details row's ordinal.
    bad = _mutated(_words("Settlement By Payment Type.xlsx"), "A13", "1.0")
    with pytest.raises(ValueError, match="'Details'.*'1.0'.*not a detail, subtotal or total row"):
        split_report(bad)


def test_a_row_with_cells_but_no_ordinal_is_refused():
    # Row 13 without its A13 ordinal has column B populated and column A empty.
    bad = _without(_words("Settlement By Payment Type.xlsx"), "A13")
    with pytest.raises(ValueError, match="'Details'.*'Reservation'.*not a detail, subtotal or total row"):
        split_report(bad)


def test_a_second_total_row_is_refused():
    # Row 19 of All Payments is blank between the C18 total and A20 END OF REPORT.
    bad = _words("All Payments.xlsx") + [_at("C19", "900")]
    with pytest.raises(ValueError, match="'All Payments - Payment Type' has two total rows"):
        split_report(bad)


def test_a_header_row_with_a_column_a_cell_is_refused():
    # Row 12 is the Details header; a cell in A12 makes it look like a detail row.
    bad = _words("Settlement By Payment Type.xlsx") + [_at("A12", "1")]
    with pytest.raises(ValueError, match=r"expected a header row from column B, found \['1', 'Account Category'"):
        split_report(bad)


def test_two_consecutive_label_rows_are_refused():
    words = _grid([
        {1: "Lodge"}, {1: "CODE"}, {1: "All Payments"},
        {2: "First"}, {2: "Second"}, {2: "Payment Type", 3: "Amount"},
        {1: "END OF REPORT"},
    ])
    with pytest.raises(ValueError, match="'First' has no header row"):
        split_report(words)


def test_a_label_directly_before_end_of_report_is_refused():
    words = _grid([{1: "Lodge"}, {1: "CODE"}, {1: "All Payments"}, {2: "Only"}, {1: "END OF REPORT"}])
    with pytest.raises(ValueError, match="'Only' has no header row"):
        split_report(words)
