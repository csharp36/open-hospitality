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

FIX = Path("tests/fixtures/hotelkey")
BD = date(2026, 8, 13)


def _words(name: str) -> list[Word]:
    return read_words(FIX / name)


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
