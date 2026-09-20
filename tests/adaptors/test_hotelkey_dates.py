from datetime import date

import pytest

from usali.adaptors.hotelkey import extract_business_date
from usali.adaptors.pdf import Word
from usali.normalize import parse_hotelkey_date


def _words(*texts: str) -> list[Word]:
    return [Word(text=t, x0=float(i), top=0.0) for i, t in enumerate(texts)]


def test_parse_hotelkey_date_with_and_without_comma():
    assert parse_hotelkey_date("Aug 13, 2026") == date(2026, 8, 13)
    assert parse_hotelkey_date("Aug 14 2026") == date(2026, 8, 14)


def test_pdf_header_date_is_the_report_date_not_the_run_date():
    # Real header order: "Date: Aug 13, 2026" on line 1, "Report Run Date: Aug 14 2026" on line 2.
    words = _words(
        "Summit", "Lodge", "Redstone,", "TX", "Date:", "Aug", "13,", "2026",
        "RDQSM", "Report", "Run", "Date:", "Aug", "14", "2026",
    )
    assert extract_business_date(words) == date(2026, 8, 13)


def test_xlsx_date_range_yields_the_range_end():
    words = _words(
        "Lakeview Inn", "Date Range: Aug 13, 2026 - Aug 14, 2026",
        "MQBWF", "Report Run Date: Aug 14 2026",
    )
    assert extract_business_date(words) == date(2026, 8, 14)


def test_xlsx_single_date():
    words = _words("Lakeview Inn", "Date: Aug 14, 2026", "MQBWF", "Report Run Date: Aug 14 2026")
    assert extract_business_date(words) == date(2026, 8, 14)


def test_only_a_run_date_is_not_a_business_date():
    words = _words("Lakeview Inn", "MQBWF", "Report Run Date: Aug 14 2026")
    with pytest.raises(ValueError, match="no HotelKey"):
        extract_business_date(words)
