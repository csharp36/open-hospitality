import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from usali.adaptors.hotelkey_hotel_statistics import parse_financial_rows, parse_hotel_statistics
from usali.adaptors.pdf import Word

BD = date(2026, 8, 13)


def _words() -> list[Word]:
    d = json.loads(Path("tests/fixtures/hotelkey_hotel_statistics_words.json").read_text())
    return [Word(text=x["text"], x0=x["x0"], top=x["top"]) for x in d]


def _stats():
    return parse_hotel_statistics(_words(), property_id="HKDEMO", business_date=BD)


def test_periods_are_canonical_with_prior_year_flags():
    by = {(r.metric_label, r.period_label, r.is_prior_year): r.value for r in _stats()}
    assert by[("Total Rooms", "DAY", False)] == Decimal("80")
    assert by[("Total Rooms", "MTD", False)] == Decimal("2480")
    assert by[("Total Rooms", "MTD", True)] == Decimal("2480")
    assert by[("Total Rooms", "YTD", False)] == Decimal("18160")
    assert by[("Total Rooms", "YTD", True)] == Decimal("18160")
    assert {r.period_label for r in _stats()} == {"DAY", "MTD", "YTD"}


def test_wrapped_labels_are_joined_so_the_four_occupancy_variants_stay_distinct():
    day = {r.metric_label: r.value for r in _stats() if r.period_label == "DAY" and not r.is_prior_year}
    assert day["Rooms Available To Sell"] == Decimal("76")
    assert day["Rooms Sold Excluding Comp House Use Rooms"] == Decimal("37")
    occupancy = {
        "Occupancy Including Out of Order Rooms, Comp, House Use Rooms": Decimal("50.00"),
        "Occupancy Excluding Out of Order Rooms, Comp, House Use Rooms": Decimal("48.68"),
        "Occupancy Including Out of Order Rooms and Excluding Comp": Decimal("47.50"),
        "Occupancy Excluding Out of Order Rooms and Including Comp, House Use Rooms": Decimal("52.63"),
    }
    assert {k: day[k] for k in occupancy} == occupancy
    assert len(set(occupancy.values())) == 4  # the fixture keeps them different on purpose
    assert day["ADR Including Comp House Use Rooms"] != day["ADR Excluding Comp House Use Rooms"]
    assert day["RevPar With Out Of Order Rooms"] != day["RevPAR"]


def test_sparse_rows_assign_by_nearest_column():
    recs = [r for r in _stats() if r.metric_label == "New Booking"]
    assert {(r.period_label, r.is_prior_year): r.value for r in recs} == {
        ("DAY", False): Decimal("30"), ("MTD", False): Decimal("900"),
        ("YTD", False): Decimal("6600"), ("YTD", True): Decimal("0"),
    }
    tomorrow = [r for r in _stats() if r.metric_label == "Tomorrows Arrivals"]
    assert [(r.period_label, r.value) for r in tomorrow] == [("DAY", Decimal("12"))]
    assert any(r.metric_label == "Guest Count For Tomorrows Arrivals" for r in _stats())


def test_financial_section_totals_are_statistics_and_their_lines_are_not():
    labels = {r.metric_label for r in _stats()}
    assert {"Room Revenue / Totals", "Misc Revenue / Totals", "Revenue Statistics / Totals",
            "Taxes / Totals", "Payments / Totals"} <= labels
    assert "Taxable Room Revenue" not in labels and "MASTER" not in labels
    day = {r.metric_label: r.value for r in _stats() if r.period_label == "DAY" and not r.is_prior_year}
    assert day["Room Revenue / Totals"] == Decimal("5000.00")
    assert day["Revenue Statistics / Totals"] == Decimal("5125.00")


def test_page_furniture_never_becomes_a_metric():
    labels = {r.metric_label for r in _stats()}
    assert not any(label.startswith("Page") or label == "Hotel Statistics" for label in labels)
    assert not any("Hotel Statistics" in label for label in labels)


def test_every_record_names_the_source_and_report():
    recs = _stats()
    assert recs and all(r.pms_source == "HOTELKEY" and r.report_type == "hotel_statistics" for r in recs)
    assert all(r.property_id == "HKDEMO" and r.business_date == BD for r in recs)


def test_financial_rows_are_day_grain_and_section_qualified():
    rows = parse_financial_rows(_words(), property_id="HKDEMO", business_date=BD)
    by = {(r.section, r.pms_trx_code): r.raw_amount for r in rows}
    assert by[("Room Revenue", "Taxable Room Revenue")] == Decimal("4800.00")
    assert by[("Misc Revenue", "DIFFERENCE DROP")] == Decimal("0.00")
    assert by[("Taxes", "EXEMPTED STATE TAX")] == Decimal("-12.00")
    assert by[("Payments", "MASTER")] == Decimal("2000.00")
    assert not any(r.pms_trx_code == "Totals" for r in rows)
    assert all(r.report_type == "hotel_statistics" and r.pms_source == "HOTELKEY" for r in rows)
    assert all(r.business_date == BD for r in rows)
    assert len(rows) == 2 + 5 + 4 + 5


def _mutate(words: list[Word], label: str, old: str, new: str) -> list[Word]:
    # Change one value token in the row whose first label token is `label`.
    from usali.adaptors.pdf import cluster_rows
    out = list(words)
    position = {id(w): i for i, w in enumerate(out)}
    for row in cluster_rows(words):
        cells = sorted(row, key=lambda w: w.x0)
        texts = [w.text for w in cells]
        if texts[0] == label and old in texts:
            target = cells[texts.index(old)]
            out[position[id(target)]] = Word(text=new, x0=target.x0, top=target.top)
            return out
    raise AssertionError(f"row {label!r} with {old!r} not in fixture")


def test_a_financial_section_that_does_not_foot_is_refused():
    words = _mutate(_words(), "MASTER", "2,000.00", "2,001.00")
    with pytest.raises(ValueError, match="Payments"):
        parse_financial_rows(words, property_id="HKDEMO", business_date=BD)


def test_a_missing_column_header_is_refused():
    words = [w for w in _words() if w.text != "LY-T-D"]
    with pytest.raises(ValueError, match="column header"):
        parse_hotel_statistics(words, property_id="HKDEMO", business_date=BD)


def test_rows_after_the_page_three_bare_header_are_parsed():
    # Page 3 opens with a header row and no heading above it; the rows under
    # it belong to the "Guest Statistics" heading that closed page 2.
    day = {r.metric_label: r.value for r in _stats() if r.period_label == "DAY" and not r.is_prior_year}
    assert day["Adults"] == Decimal("45")
    assert day["Total Guests"] == Decimal("48")
    assert day["Departures"] == Decimal("14")


def test_two_values_under_one_column_are_refused():
    header = [Word(text=t, x0=x, top=100.0) for t, x in [
        ("Description", 46.0), ("Actual", 134.0), ("Today", 160.0), ("M-T-D", 240.0),
        ("LY-M-T-D", 326.0), ("Y-T-D", 426.0), ("LY-T-D", 516.0)]]
    row = [Word(text=t, x0=x, top=114.0) for t, x in [
        ("Total", 43.0), ("Rooms", 63.0), ("80", 150.0), ("81", 165.0), ("2,480", 242.0),
        ("2,480", 334.0), ("18,160", 424.0), ("18,160", 517.0)]]
    with pytest.raises(ValueError, match="two values under one column"):
        parse_hotel_statistics(header + row, property_id="HKDEMO", business_date=BD)
