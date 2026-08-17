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


def test_sparse_row_uses_nearest_column_assignment():
    # A sparse metric row populates only TWO of the five value columns, at OFF-CENTRE
    # x0 (not exactly on an anchor). This exercises nearest-column assignment, which an
    # ordinal-zip would get wrong: 312 must land in ACTUAL@300 (not PTD@380) and 548 in
    # YTD@540 (not LY_YTD@620). No spurious records for the three empty columns.
    words = [
        Word(text="PTD", x0=300.0, top=50.0),
        Word(text="PTD1", x0=380.0, top=50.0),
        Word(text="LYPTD", x0=460.0, top=50.0),
        Word(text="YTD", x0=540.0, top=50.0),
        Word(text="LYYTD", x0=620.0, top=50.0),
        Word(text="Sparse", x0=75.0, top=70.0),
        Word(text="Metric", x0=115.0, top=70.0),
        Word(text="7", x0=312.0, top=70.0),
        Word(text="9", x0=548.0, top=70.0),
    ]
    recs = parse_hotel_statistics(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    metric = [r for r in recs if r.metric_label == "Sparse Metric"]
    by_period = {r.period_label: r.value for r in metric}
    assert by_period == {"ACTUAL": Decimal("7"), "YTD": Decimal("9")}
    assert not any(r.period_label in ("PTD", "LY_PTD", "LY_YTD") for r in metric)


def test_extract_business_date():
    assert extract_business_date(_words()) == date(2026, 6, 21)
