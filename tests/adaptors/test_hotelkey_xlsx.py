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


def _mutated(words: list[Word], cell: str, text: str) -> list[Word]:
    """Replace the one word at sheet cell ``cell`` (e.g. "N20") with ``text``."""
    col, row = ord(cell[0]) - ord("A") + 1, int(cell[1:])
    x0, top = col * XLSX_COL_STEP, row * XLSX_ROW_STEP
    hits = [w for w in words if w.x0 == x0 and w.top == top]
    assert len(hits) == 1, (cell, hits)
    return [Word(text=text, x0=w.x0, top=w.top) if w is hits[0] else w for w in words]


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
    words = _words("Settlement By Payment Type.xlsx")
    bad = [Word(text="701", x0=w.x0, top=w.top) if (w.text == "700" and w.top > 200) else w for w in words]
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
    words = _words("AR Invoice Aging.xlsx")
    # Row 31 is the Company Name section's total row (generator row map: 27 label,
    # 28 header, 29-30 rows, 31 total); XLSX_ROW_STEP puts it at top 310.0.
    bad = [Word(text="3401", x0=w.x0, top=w.top) if (w.text == "3400" and w.top == 310.0) else w for w in words]
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
    with pytest.raises(ValueError, match="Summary total disagrees"):
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
    # B23 is the "Summary" label; without it the Summary rows fall under Details
    # and the section lookup is what refuses.
    words = _words("Settlement By Payment Type.xlsx")
    x0, top = 2 * XLSX_COL_STEP, 23 * XLSX_ROW_STEP
    assert [w.text for w in words if w.x0 == x0 and w.top == top] == ["Summary"]
    without = [w for w in words if not (w.x0 == x0 and w.top == top)]
    with pytest.raises(ValueError, match="has no 'Summary' section"):
        parse_settlement(without, property_id="HKDEMO", business_date=BD)
