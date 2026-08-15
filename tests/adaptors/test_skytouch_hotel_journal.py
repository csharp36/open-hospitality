import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from usali.adaptors.pdf import Word
from usali.adaptors.skytouch_hotel_journal import (
    extract_business_date,
    parse_hotel_journal,
)


def _words():
    d = json.loads(
        Path("tests/fixtures/skytouch_hotel_journal_words.json").read_text()
    )
    return [Word(text=x["text"], x0=x["x0"], top=x["top"]) for x in d]


def test_parses_codes_and_paren_negatives():
    recs = parse_hotel_journal(
        _words(), property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    by = {r.pms_trx_code: r for r in recs}
    assert by["RM"].raw_amount == Decimal("4000.00")
    assert by["CA"].raw_amount == Decimal("-500.00")
    assert by["VI"].raw_amount == Decimal("-3750.00")
    assert by["T1"].raw_amount == Decimal("250.00")
    assert all(
        r.pms_source == "SKYTOUCH" and r.report_type == "hotel_journal" for r in recs
    )
    assert "Today's Total" not in " ".join((r.pms_trx_desc or "") for r in recs)


def test_reconciles_to_zero_and_extracts_date():
    words = _words()
    recs = parse_hotel_journal(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    assert sum(r.raw_amount for r in recs) == Decimal("0.00")
    assert extract_business_date(words) == date(2026, 6, 21)
