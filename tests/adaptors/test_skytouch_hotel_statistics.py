import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from usali.adaptors.pdf import Word
from usali.adaptors.skytouch_hotel_statistics import (
    extract_business_date,
    parse_hotel_statistics,
)


def _words():
    d = json.loads(
        Path("tests/fixtures/skytouch_hotel_statistics_words.json").read_text()
    )
    return [Word(text=x["text"], x0=x["x0"], top=x["top"]) for x in d]


def test_parses_metrics_by_period():
    recs = parse_hotel_statistics(
        _words(), property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    by = {(r.metric_label, r.period_label): r.value for r in recs}
    assert by[("Total Rooms", "ACTUAL")] == Decimal("100")
    assert by[("ADR for Total Occupied Rooms", "ACTUAL")] == Decimal("88.50")
    assert by[("RevPar", "ACTUAL")] == Decimal("54.87")
    assert by[("Total Room Revenue", "YTD")] == Decimal("442520.00")


def test_source_report_type_and_prior_year_flag():
    recs = parse_hotel_statistics(
        _words(), property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    assert recs and all(
        r.pms_source == "SKYTOUCH" and r.report_type == "hotel_statistics"
        for r in recs
    )
    ly = [r for r in recs if r.period_label in ("LY_PTD", "LY_YTD")]
    assert ly and all(r.is_prior_year for r in ly)
    non_ly = [r for r in recs if r.period_label in ("ACTUAL", "PTD", "YTD")]
    assert non_ly and all(not r.is_prior_year for r in non_ly)


def test_extract_business_date():
    assert extract_business_date(_words()) == date(2026, 6, 21)
