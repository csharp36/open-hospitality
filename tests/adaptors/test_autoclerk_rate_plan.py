import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from usali.adaptors.autoclerk_rate_plan import extract_business_date, parse_rate_plan
from usali.adaptors.pdf import Word

FIXTURE = "tests/fixtures/autoclerk_rate_plan_words.json"


def _load_words() -> list[Word]:
    data = json.loads(Path(FIXTURE).read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def _index(recs):
    return {(r.segment_code, r.measure): r.value for r in recs}


def test_extract_business_date():
    assert extract_business_date(_load_words()) == date(2026, 7, 7)


def test_parses_rate_code_rows():
    recs = parse_rate_plan(_load_words(), property_id="SSSJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("15C", "ROOMS")] == Decimal("3")
    assert v[("15C", "ADR")] == Decimal("118.83")
    assert v[("15C", "ROOM_REVENUE")] == Decimal("356.49")
    assert v[("RACK", "ROOMS")] == Decimal("9")
    assert v[("RACK", "ROOM_REVENUE")] == Decimal("1111.49")
    assert v[("RACK", "NONROOM_REVENUE")] == Decimal("230.00")
    assert v[("CLC", "ROOM_REVENUE")] == Decimal("75.00")


def test_totals_row_emitted_as_TOTAL():
    recs = parse_rate_plan(_load_words(), property_id="SSSJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("TOTAL", "ROOMS")] == Decimal("50")
    assert v[("TOTAL", "ADR")] == Decimal("122.58")
    assert v[("TOTAL", "ROOM_REVENUE")] == Decimal("6129.03")
    assert v[("TOTAL", "TOTAL_REVENUE")] == Decimal("6359.03")


def test_all_records_are_day_period_and_tagged():
    recs = parse_rate_plan(_load_words(), property_id="SSSJ", business_date=date(2026, 7, 7))
    assert recs
    assert all(r.period_label == "DAY" for r in recs)
    assert all(r.pms_source == "AUTOCLERK" and r.report_type == "rate_plan" for r in recs)
    codes = {r.segment_code for r in recs}
    assert len(codes) == 22  # 21 rate codes + TOTAL


def test_reconciliation_mismatch_raises():
    words = [
        Word(text="RACK", x0=28.0, top=100.0),
        Word(text="9", x0=189.0, top=100.0),
        Word(text="$", x0=209.0, top=100.0),
        Word(text="123.50", x0=217.0, top=100.0),
        Word(text="$", x0=271.0, top=100.0),
        Word(text="1,111.49", x0=279.0, top=100.0),
        Word(text="18.1%", x0=336.0, top=100.0),
        Word(text="$", x0=387.0, top=100.0),
        Word(text="230.00", x0=395.0, top=100.0),
        Word(text="100.0%", x0=437.0, top=100.0),
        Word(text="$", x0=489.0, top=100.0),
        Word(text="1,341.49", x0=497.0, top=100.0),
        Word(text="21.1%", x0=556.0, top=100.0),
        Word(text="TOTALS:", x0=113.7, top=120.0),
        Word(text="99", x0=184.0, top=120.0),  # wrong rooms total
        Word(text="$122.58", x0=212.0, top=120.0),
        Word(text="$", x0=271.0, top=120.0),
        Word(text="9,999.99", x0=279.0, top=120.0),  # wrong revenue total
        Word(text="$", x0=387.0, top=120.0),
        Word(text="230.00", x0=395.0, top=120.0),
        Word(text="$", x0=489.0, top=120.0),
        Word(text="6,359.03", x0=497.0, top=120.0),
    ]
    with pytest.raises(ValueError, match="!="):
        parse_rate_plan(words, property_id="X", business_date=date(2026, 7, 7))


def test_missing_totals_row_raises():
    words = [
        Word(text="RACK", x0=28.0, top=100.0),
        Word(text="9", x0=189.0, top=100.0),
        Word(text="$", x0=209.0, top=100.0),
        Word(text="123.50", x0=217.0, top=100.0),
    ]
    with pytest.raises(ValueError, match="TOTALS"):
        parse_rate_plan(words, property_id="X", business_date=date(2026, 7, 7))
