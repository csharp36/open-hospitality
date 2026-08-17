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


# Header-derived column anchor: "Postings" at x0=300, next column at 370 (spacing 70,
# so an amount is "in" the Postings column when it is within 35px of x0=300).
def _header(top=5.0):
    return _row(
        top,
        [(300.0, "Postings"), (370.0, "Corrections"), (440.0, "Adjustments"),
         (510.0, "Totals")],
    )


def test_reconciliation_mismatch_raises():
    # Postings sum to 500.00 but Today's Total claims 999.00 — must fail closed.
    words = [
        *_header(),
        *_row(10.0, [(75.0, "Room"), (115.0, "Charge"), (250.0, "(RM)"), (300.0, "500.00")]),
        *_row(30.0, [(75.0, "Today's"), (115.0, "Total:"), (300.0, "999.00")]),
    ]
    with pytest.raises(ValueError, match="!= Today's Total"):
        parse_hotel_journal(words, property_id="STDEMO", business_date=date(2026, 6, 21))


def test_amount_anchored_to_postings_column_not_description():
    # A rate token "12.99" sits in the description (far left of the Postings column);
    # the Postings money "500.00" sits on the header anchor. raw_amount must be 500.00,
    # never 12.99 — the column anchor rejects the stray left-hand token.
    words = [
        *_header(),
        *_row(10.0, [(75.0, "Rate"), (115.0, "12.99"), (250.0, "(XY)"), (300.0, "500.00")]),
        *_row(30.0, [(75.0, "Today's"), (115.0, "Total:"), (300.0, "500.00")]),
    ]
    recs = parse_hotel_journal(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    by = {r.pms_trx_code: r for r in recs}
    assert by["XY"].raw_amount == Decimal("500.00")


def test_empty_postings_cell_not_promoted_from_corrections():
    # A corrections-only line: (CR) has a value ONLY in the Corrections column (x0=370)
    # and an EMPTY Postings cell. That 250.00 must NOT be staged as a Postings record —
    # the row simply contributes nothing to Postings. (RM) carries the only real Postings
    # value, and the reconciliation ties to it alone.
    words = [
        *_header(),
        *_row(10.0, [(75.0, "Room"), (115.0, "Charge"), (250.0, "(RM)"), (300.0, "500.00")]),
        *_row(30.0, [(75.0, "Fee"), (250.0, "(CR)"), (370.0, "250.00")]),
        *_row(50.0, [(75.0, "Today's"), (115.0, "Total:"), (300.0, "500.00")]),
    ]
    recs = parse_hotel_journal(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )
    codes = {r.pms_trx_code for r in recs}
    assert codes == {"RM"}
    assert "CR" not in codes
    # The Corrections value never leaks into any staged amount.
    assert all(r.raw_amount != Decimal("250.00") for r in recs)


def test_missing_postings_header_raises():
    # No "Postings" column header => the money column is unanchorable => fail closed.
    words = [
        *_row(10.0, [(75.0, "Room"), (115.0, "Charge"), (250.0, "(RM)"), (300.0, "500.00")]),
        *_row(30.0, [(75.0, "Today's"), (115.0, "Total:"), (300.0, "500.00")]),
    ]
    with pytest.raises(ValueError, match="Postings"):
        parse_hotel_journal(words, property_id="STDEMO", business_date=date(2026, 6, 21))


def test_missing_total_row_raises():
    words = [
        *_header(),
        *_row(10.0, [(75.0, "Room"), (115.0, "Charge"), (250.0, "(RM)"), (300.0, "500.00")]),
    ]
    with pytest.raises(ValueError, match="no Today's Total row"):
        parse_hotel_journal(words, property_id="STDEMO", business_date=date(2026, 6, 21))
