import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

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


def _row(top, cells):
    """Build a row of Words from (x0, text) cells at a given vertical position."""
    return [Word(text=t, x0=x0, top=top) for x0, t in cells]


def test_reconciliation_mismatch_raises():
    # Postings sum to 500.00 but Today's Total claims 999.00 — must fail closed.
    words = [
        *_row(10.0, [(75.0, "Room"), (115.0, "Charge"), (250.0, "(RM)"), (300.0, "500.00")]),
        *_row(30.0, [(75.0, "Today's"), (115.0, "Total:"), (300.0, "999.00")]),
    ]
    with pytest.raises(ValueError, match="!= Today's Total"):
        parse_hotel_journal(words, property_id="STDEMO", business_date=date(2026, 6, 21))


def test_amount_anchored_to_column_after_code_not_description():
    # A rate token "12.99" sits in the description (left of the code); the Postings
    # money "500.00" sits right of the code. raw_amount must be 500.00, never 12.99.
    words = [
        *_row(10.0, [(75.0, "Rate"), (115.0, "12.99"), (250.0, "(XY)"), (300.0, "500.00")]),
        *_row(30.0, [(75.0, "Today's"), (115.0, "Total:"), (300.0, "500.00")]),
    ]
    recs = parse_hotel_journal(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    by = {r.pms_trx_code: r for r in recs}
    assert by["XY"].raw_amount == Decimal("500.00")


def test_missing_total_row_raises():
    words = _row(10.0, [(75.0, "Room"), (115.0, "Charge"), (250.0, "(RM)"), (300.0, "500.00")])
    with pytest.raises(ValueError, match="no Today's Total row"):
        parse_hotel_journal(words, property_id="STDEMO", business_date=date(2026, 6, 21))
