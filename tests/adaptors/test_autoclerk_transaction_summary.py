import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from usali.adaptors.autoclerk_transaction_summary import (
    extract_business_date,
    parse_transaction_summary,
)
from usali.adaptors.pdf import Word


def _col_header() -> list[Word]:
    return [
        Word(text="Category", x0=28.0, top=10.0),
        Word(text="Type", x0=132.0, top=10.0),
        Word(text="TODAY", x0=311.0, top=10.0),
    ]


def _type_row(top: float, name: str, amount: str) -> list[Word]:
    return [
        Word(text=name, x0=132.0, top=top),
        Word(text="$", x0=298.0, top=top),
        Word(text=amount, x0=307.0, top=top),
    ]

FIXTURE = "tests/fixtures/autoclerk_transaction_summary_words.json"


def _load_words() -> list[Word]:
    data = json.loads(Path(FIXTURE).read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def test_extract_business_date():
    assert extract_business_date(_load_words()) == date(2026, 7, 7)


def test_parses_synthetic_codes_and_today_amounts():
    recs = parse_transaction_summary(
        _load_words(), property_id="SSSJ", business_date=date(2026, 7, 7)
    )
    by_code = {r.pms_trx_code: r for r in recs}

    assert by_code["ROOM|ROOM_RENT"].raw_amount == Decimal("6129.03")
    assert by_code["ROOM|ROOM_RENT"].section == "Room"
    assert by_code["CREDIT_CARDS|AMERICAN_EXPRESS"].raw_amount == Decimal("-428.35")
    assert by_code["TAX|OCCUPANCY_TAX"].raw_amount == Decimal("612.84")
    assert by_code["MISC|SMOKING"].raw_amount == Decimal("200.00")


def test_all_records_are_autoclerk_and_have_pipe_codes():
    recs = parse_transaction_summary(
        _load_words(), property_id="SSSJ", business_date=date(2026, 7, 7)
    )
    assert recs
    assert all(r.pms_source == "AUTOCLERK" for r in recs)
    assert all(r.report_type == "transaction_summary" for r in recs)
    assert all("|" in r.pms_trx_code for r in recs)


def test_no_total_rows_leak_in():
    recs = parse_transaction_summary(
        _load_words(), property_id="SSSJ", business_date=date(2026, 7, 7)
    )
    assert all(not r.pms_trx_code.split("|")[1].startswith("TOTAL") for r in recs)
    assert all("GRAND" not in r.pms_trx_code for r in recs)


def test_parser_reconciles_against_grand_total():
    recs = parse_transaction_summary(
        _load_words(), property_id="SSSJ", business_date=date(2026, 7, 7)
    )
    assert sum((r.raw_amount for r in recs), Decimal("0")) == Decimal("438.77")


def test_reconciliation_mismatch_raises():
    # Type rows sum to 100.00, but the GRAND TOTAL claims 999.00 -> must raise.
    words = [
        *_col_header(),
        Word(text="Room", x0=28.0, top=30.0),  # category header
        *_type_row(50.0, "Rent", "100.00"),
        Word(text="GRAND", x0=169.0, top=70.0),
        Word(text="TOTAL:", x0=208.0, top=70.0),
        Word(text="$", x0=307.0, top=70.0),
        Word(text="999.00", x0=315.0, top=70.0),
    ]
    with pytest.raises(ValueError, match="!="):
        parse_transaction_summary(words, property_id="X", business_date=date(2026, 7, 7))


def test_missing_grand_total_raises():
    # No GRAND TOTAL row -> the parse cannot be verified -> must raise loudly, not skip.
    words = [
        *_col_header(),
        Word(text="Room", x0=28.0, top=30.0),
        *_type_row(50.0, "Rent", "100.00"),
    ]
    with pytest.raises(ValueError, match="GRAND TOTAL"):
        parse_transaction_summary(words, property_id="X", business_date=date(2026, 7, 7))
