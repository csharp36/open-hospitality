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
    # ordinal-zip would get wrong: 232 must land in ACTUAL@221.85 (not PTD@317.61) and
    # 468 in YTD@459.67 (not LY_YTD@531.65). No records for the three empty columns.
    #
    # Header is the REAL shape: the anchors are the business date and each of the two
    # PTD / two YTD tokens, so the multi-word wrapping around them is irrelevant.
    words = [
        Word(text="Room", x0=38.19, top=50.0),
        Word(text="Statistics", x0=61.25, top=50.0),
        Word(text="6/21/2026", x0=221.85, top=50.0),
        Word(text="Current", x0=289.00, top=50.0),
        Word(text="PTD", x0=317.61, top=50.0),
        Word(text="Last", x0=352.69, top=50.0),
        Word(text="Year", x0=370.04, top=50.0),
        Word(text="PTD", x0=388.10, top=50.0),
        Word(text="Current", x0=431.00, top=50.0),
        Word(text="YTD", x0=459.67, top=50.0),
        Word(text="Last", x0=514.30, top=50.0),
        Word(text="YTD", x0=531.65, top=50.0),
        Word(text="Sparse", x0=38.19, top=70.0),
        Word(text="Metric", x0=68.19, top=70.0),
        Word(text="7", x0=232.0, top=70.0),
        Word(text="9", x0=468.0, top=70.0),
    ]
    recs = parse_hotel_statistics(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    metric = [r for r in recs if r.metric_label == "Sparse Metric"]
    by_period = {r.period_label: r.value for r in metric}
    assert by_period == {"ACTUAL": Decimal("7"), "YTD": Decimal("9")}
    assert not any(r.period_label in ("PTD", "LY_PTD", "LY_YTD") for r in metric)


def test_header_without_the_current_prefix_also_anchors():
    """The other real variant: "Room Statistics" omits "Current" before PTD/YTD
    while every later section includes it. Same five column positions, so the
    shape rule must accept both."""
    words = [
        Word(text="Room", x0=38.19, top=50.0),
        Word(text="Statistics", x0=61.25, top=50.0),
        Word(text="6/21/2026", x0=221.85, top=50.0),
        Word(text="PTD", x0=317.61, top=50.0),
        Word(text="Last", x0=352.69, top=50.0),
        Word(text="Year", x0=370.04, top=50.0),
        Word(text="PTD", x0=388.10, top=50.0),
        Word(text="YTD", x0=459.67, top=50.0),
        Word(text="Last", x0=514.30, top=50.0),
        Word(text="YTD", x0=531.65, top=50.0),
        Word(text="Total", x0=38.19, top=70.0),
        Word(text="Rooms", x0=68.19, top=70.0),
        Word(text="120", x0=246.0, top=70.0),
        Word(text="2,520", x0=313.0, top=70.0),
        Word(text="2,512", x0=384.0, top=70.0),
        Word(text="20,640", x0=450.0, top=70.0),
        Word(text="20,481", x0=522.0, top=70.0),
    ]
    recs = parse_hotel_statistics(
        words, property_id="NM070", business_date=date(2026, 6, 21)
    )
    by_period = {r.period_label: r.value for r in recs if r.metric_label == "Total Rooms"}
    assert by_period == {
        "ACTUAL": Decimal("120"),
        "PTD": Decimal("2520"),
        "LY_PTD": Decimal("2512"),
        "YTD": Decimal("20640"),
        "LY_YTD": Decimal("20481"),
    }


def test_extract_business_date():
    assert extract_business_date(_words()) == date(2026, 6, 21)
