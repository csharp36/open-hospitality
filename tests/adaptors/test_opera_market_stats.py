import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from usali.adaptors.opera_market_stats import parse_market_stats
from usali.adaptors.pdf import Word

FIXTURE = "tests/fixtures/opera_market_stats_words.json"


def _load_words() -> list[Word]:
    data = json.loads(Path(FIXTURE).read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def _index(recs):
    return {(r.segment_code, r.measure, r.period_label): r.value for r in recs}


def test_parses_market_code_rows():
    recs = parse_market_stats(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("D", "ROOMS", "DAY")] == Decimal("35")
    assert v[("D", "ROOM_REVENUE", "DAY")] == Decimal("6047.33")
    assert v[("D", "OCC_PCT", "DAY")] == Decimal("42.68")
    assert v[("D", "ROOM_REVENUE", "YEAR")] == Decimal("887034.57")
    assert v[("G", "ROOM_REVENUE", "DAY")] == Decimal("2516.95")


def test_complimentary_year_band_disambiguates_revenue_and_adr():
    recs = parse_market_stats(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("N", "ROOM_REVENUE", "YEAR")] == Decimal("4847.00")
    assert v[("N", "ADR", "YEAR")] == Decimal("4847.00")
    assert v[("N", "ROOMS", "YEAR")] == Decimal("1")


def test_grand_total_emitted_and_group_totals_skipped():
    recs = parse_market_stats(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("TOTAL", "ROOMS", "DAY")] == Decimal("62")
    assert v[("TOTAL", "ROOM_REVENUE", "DAY")] == Decimal("10395.00")
    assert v[("TOTAL", "ROOM_REVENUE", "YEAR")] == Decimal("2021427.81")
    # Group Total rows must not appear as a segment code.
    codes = {r.segment_code for r in recs}
    assert codes == {"D", "G", "H", "J", "K", "L", "M", "N", "P", "V", "W", "X", "Y", "Z", "TOTAL"}


def test_all_records_tagged():
    recs = parse_market_stats(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    assert recs
    assert all(r.pms_source == "OPERA" and r.report_type == "market_stats" for r in recs)
