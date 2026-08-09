import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from usali.adaptors.opera_manager_flash import parse_manager_flash
from usali.adaptors.pdf import Word

FIXTURE = "tests/fixtures/opera_manager_flash_words.json"


def _load_words() -> list[Word]:
    data = json.loads(Path(FIXTURE).read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def _index(recs):
    return {(r.metric_label, r.period_label, r.is_prior_year): r.value for r in recs}


def test_parses_current_and_prior_year_columns():
    recs = parse_manager_flash(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    assert v[("% Rooms Occupied", "DAY", False)] == Decimal("75.61")
    assert v[("% Rooms Occupied", "MONTH", False)] == Decimal("66.90")
    assert v[("% Rooms Occupied", "YEAR", False)] == Decimal("80.73")
    assert v[("% Rooms Occupied", "DAY", True)] == Decimal("79.27")
    assert v[("ADR", "DAY", False)] == Decimal("167.66")
    assert v[("Total Revenue", "YEAR", False)] == Decimal("2117486.96")
    assert v[("Rooms Occupied", "YEAR", True)] == Decimal("12221")
    assert v[("No Show Rooms", "MONTH", True)] == Decimal("3")


def test_sparse_rows_assign_by_column_position():
    recs = parse_manager_flash(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    v = _index(recs)
    # "% Rooms Occupied for Tomorrow" has values only in DAY (current) and DAY (prior).
    assert v[("% Rooms Occupied for Tomorrow", "DAY", False)] == Decimal("53.66")
    assert v[("% Rooms Occupied for Tomorrow", "DAY", True)] == Decimal("92.68")
    assert ("% Rooms Occupied for Tomorrow", "MONTH", False) not in v


def test_all_records_tagged():
    recs = parse_manager_flash(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    assert recs
    assert all(r.pms_source == "OPERA" and r.report_type == "manager_flash" for r in recs)
