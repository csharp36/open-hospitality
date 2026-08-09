import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from usali.adaptors.opera_trial_balance import parse_trial_balance
from usali.adaptors.pdf import Word, cluster_rows


def _load_words() -> list[Word]:
    data = json.loads(Path("tests/fixtures/opera_trial_balance_words.json").read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def test_parses_known_codes():
    records = parse_trial_balance(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    by_code = {r.pms_trx_code: r for r in records}

    assert by_code["1000"].raw_amount == Decimal("10395.00")
    assert by_code["1000"].section == "Revenue"
    assert "Accommodation" in (by_code["1000"].pms_trx_desc or "")

    assert by_code["9004"].raw_amount == Decimal("-3496.32")
    assert by_code["9004"].section == "Payment"

    assert by_code["7100"].section == "Non Revenue"
    assert by_code["7100"].raw_amount == Decimal("1039.59")


def test_all_records_have_source_and_report_type():
    records = parse_trial_balance(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    assert records
    assert all(r.pms_source == "OPERA" and r.report_type == "trial_balance" for r in records)


def test_no_total_or_header_rows_leak_in():
    records = parse_trial_balance(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    # Every parsed row must have a numeric transaction code.
    assert all(r.pms_trx_code.isdigit() for r in records)


def test_parses_exactly_the_active_codes():
    records = parse_trial_balance(_load_words(), property_id="HISJ", business_date=date(2026, 7, 7))
    assert [r.pms_trx_code for r in records] == [
        "1000", "5105", "5106", "5107", "5007",
        "7100", "7101", "7102", "7104",
        "9002", "9003", "9004", "9005", "9007",
    ]


def test_cluster_rows_tolerates_cumulative_drift():
    # Three tokens whose tops drift 2pt each (cumulative 4pt > y_tol) still form ONE row,
    # because each is within y_tol of its neighbour; a far-away token starts a new row.
    words = [
        Word(text="a", x0=10.0, top=100.0),
        Word(text="b", x0=20.0, top=102.0),
        Word(text="c", x0=30.0, top=104.0),
        Word(text="far", x0=10.0, top=120.0),
    ]
    rows = cluster_rows(words, y_tol=3.0)
    assert len(rows) == 2
    assert sorted(w.text for w in rows[0]) == ["a", "b", "c"]
    assert [w.text for w in rows[1]] == ["far"]
