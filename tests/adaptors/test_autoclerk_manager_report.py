import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from usali.adaptors.autoclerk_manager_report import (
    extract_business_date,
    parse_manager_report,
)
from usali.adaptors.pdf import Word

FIXTURE = "tests/fixtures/autoclerk_manager_report_words.json"


def _load_words() -> list[Word]:
    data = json.loads(Path(FIXTURE).read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def _index(recs):
    return {(r.metric_label, r.period_label): r.value for r in recs}


def test_extract_business_date():
    assert extract_business_date(_load_words()) == date(2026, 7, 7)


def test_parses_occupancy_section():
    recs = parse_manager_report(_load_words(), property_id="SSSJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("Occupancy - ADR", "Today")] == Decimal("122.58")
    assert v[("Occupancy - ADR", "MTD")] == Decimal("119.23")
    assert v[("Occupancy - ADR", "YTD")] == Decimal("115.98")
    assert v[("Occupancy - REVPAR", "Today")] == Decimal("107.53")
    assert v[("Occupancy - Occupied Percent", "Today")] == Decimal("87.72")
    assert v[("Occupancy - Occupied", "YTD")] == Decimal("8788")


def test_parses_statistics_section_with_its_own_columns():
    recs = parse_manager_report(_load_words(), property_id="SSSJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("Statistics - Arrivals", "Today")] == Decimal("24")
    assert v[("Statistics - Arrivals", "Yesterday")] == Decimal("30")
    assert v[("Statistics - Departures", "Today")] == Decimal("23")
    # "n/a" values are skipped, not zero.
    assert ("Statistics - No Shows", "Tomorrow") not in v


def test_ledger_and_forecast_blocks_are_not_captured():
    recs = parse_manager_report(_load_words(), property_id="SSSJ", business_date=date(2026, 7, 7))
    labels = {r.metric_label for r in recs}
    # The Ledger summary block and Forecast table have no recognized column labels;
    # their rows must not be captured under a stale previous section.
    assert not any("Guest Ledger" in label for label in labels)
    assert not any("Ledger Totals" in label for label in labels)
    assert not any(label.startswith("Forecast") for label in labels)


def test_all_records_tagged_and_no_prior_year():
    recs = parse_manager_report(_load_words(), property_id="SSSJ", business_date=date(2026, 7, 7))
    assert recs
    assert all(r.pms_source == "AUTOCLERK" and r.report_type == "manager_report" for r in recs)
    assert all(r.is_prior_year is False for r in recs)
