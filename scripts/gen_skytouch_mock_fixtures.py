#!/usr/bin/env python3
"""Generate SYNTHETIC SkyTouch PMS mock fixtures for adapter unit tests.

This script is the single source of truth for the SkyTouch test fixtures. It
emits two JSON "word" fixtures (shaped ``{"text", "x0", "top"}`` to feed
``usali.adaptors.pdf.Word``) and a multi-page mock "Standard Audit Pack" PDF.

EVERYTHING produced here is invented. No real guest names, card numbers, or
figures from any real SkyTouch export appear in this file or its outputs.

Run with::

    uv run python scripts/gen_skytouch_mock_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
SAMPLES_DIR = REPO_ROOT / "docs" / "reference" / "samples"

JOURNAL_FIXTURE = FIXTURES_DIR / "skytouch_hotel_journal_words.json"
STATISTICS_FIXTURE = FIXTURES_DIR / "skytouch_hotel_statistics_words.json"
PACK_PDF = SAMPLES_DIR / "SkyTouch - Standard Audit Pack (mock).pdf"

# --- Synthetic constants shared by fixtures and the mock PDF -----------------

PROPERTY_NAME = "Redstone Test Inn"
PROPERTY_CODE = "TEST1"
BUSINESS_DATE = "6/21/2026"

LABEL_X0 = 75.0
LABEL_STEP = 40.0
CODE_X0 = 250.0

# 7 amount columns for the Hotel Journal Summary.
JOURNAL_COLS = [300.0, 370.0, 440.0, 510.0, 580.0, 650.0, 720.0]

# 5 value columns for the Hotel Statistics report.
STATS_COLS = [300.0, 380.0, 460.0, 540.0, 620.0]

# (label, transaction-code token, [7 amounts left-to-right]).
JOURNAL_ROWS: list[tuple[str, str | None, list[str]]] = [
    ("Cash", "(CA)", ["(500.00)", "0.00", "0.00", "(500.00)", "(500.00)", "0.00", "0.00"]),
    ("Room Charge", "(RM)", ["4000.00", "0.00", "0.00", "4000.00", "4000.00", "0.00", "0.00"]),
    ("State Tax", "(T1)", ["250.00", "0.00", "0.00", "250.00", "250.00", "0.00", "0.00"]),
    ("Visa Payment", "(VI)",
     ["(3750.00)", "0.00", "0.00", "(3750.00)", "(3750.00)", "0.00", "0.00"]),
    ("Today's Total:", None, ["0.00", "0.00", "0.00", "0.00", "0.00", "0.00", "0.00"]),
]

# Clean single-word column anchors: ACTUAL / PTD / LY_PTD / YTD / LY_YTD.
STATS_ANCHORS = ["PTD", "PTD1", "LYPTD", "YTD", "LYYTD"]

# (label, [5 values left-to-right]).
# Fully invented, internally-consistent block. The ACTUAL column ties by
# construction: ADR 88.50 x 62 occupied rooms = 5487.00 room revenue, and
# 5487.00 / 100 total rooms = 54.87 RevPar (keep these ACTUAL values exact so
# a future reconciliation check is satisfiable).
STATISTICS_ROWS: list[tuple[str, list[str]]] = [
    ("Total Rooms", ["100", "2100", "2100", "12600", "12600"]),
    ("Total Occupied Rooms", ["62", "1300", "1250", "7800", "7600"]),
    ("ADR for Total Occupied Rooms", ["88.50", "90.10", "92.25", "91.40", "93.75"]),
    ("RevPar", ["54.87", "55.75", "57.10", "56.30", "58.20"]),
    ("Total Room Revenue",
     ["5487.00", "117130.00", "115312.50", "442520.00", "435800.00"]),
]


def _word(text: str, x0: float, top: float) -> dict[str, object]:
    return {"text": text, "x0": round(float(x0), 2), "top": round(float(top), 2)}


def _header_words(top: float) -> list[dict[str, object]]:
    """Two synthetic property/date header lines shared by both reports.

    ``BUSINESS_DATE`` is emitted as a standalone word so a date parser can find
    it regardless of surrounding tokens.
    """
    words: list[dict[str, object]] = []
    line1 = ["Property", "Name:", "Redstone", "Test", "Inn"]
    for i, tok in enumerate(line1):
        words.append(_word(tok, LABEL_X0 + i * 50.0, top))
    line2 = ["Business", "Date:", BUSINESS_DATE, "Property", "Code:", PROPERTY_CODE]
    for i, tok in enumerate(line2):
        words.append(_word(tok, LABEL_X0 + i * 50.0, top + 20.0))
    return words


def _label_words(label: str, top: float) -> list[dict[str, object]]:
    """Split a label into tokens laid out at x0=75, +40 each."""
    words: list[dict[str, object]] = []
    for i, tok in enumerate(label.split(" ")):
        words.append(_word(tok, LABEL_X0 + i * LABEL_STEP, top))
    return words


def build_journal_words() -> list[dict[str, object]]:
    words: list[dict[str, object]] = []
    words.extend(_header_words(10.0))
    # Cosmetic column-header row (single-word tokens to avoid muddying anchors).
    headers = ["Postings", "Corrections", "Adjustments", "Totals",
               "GuestLedger", "ARLedger", "AdvDepLedger"]
    for name, x0 in zip(headers, JOURNAL_COLS):
        words.append(_word(name, x0, 50.0))
    top = 70.0
    for label, code, amounts in JOURNAL_ROWS:
        words.extend(_label_words(label, top))
        if code is not None:
            words.append(_word(code, CODE_X0, top))
        for amount, x0 in zip(amounts, JOURNAL_COLS):
            words.append(_word(amount, x0, top))
        top += 20.0
    return words


def build_statistics_words() -> list[dict[str, object]]:
    words: list[dict[str, object]] = []
    words.extend(_header_words(10.0))
    # Column-anchor header row: clean single-word period tokens.
    for anchor, x0 in zip(STATS_ANCHORS, STATS_COLS):
        words.append(_word(anchor, x0, 50.0))
    top = 70.0
    for label, values in STATISTICS_ROWS:
        words.extend(_label_words(label, top))
        for value, x0 in zip(values, STATS_COLS):
            words.append(_word(value, x0, top))
        top += 20.0
    return words


def write_json(path: Path, words: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(words, indent=1) + "\n")


def build_pack_pdf(path: Path) -> None:
    """Render a synthetic multi-page audit pack with reportlab (Courier)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = letter
    c = canvas.Canvas(str(path), pagesize=letter)
    left = 60.0
    line_h = 16.0

    def draw_page(lines: list[str]) -> None:
        c.setFont("Courier", 10)
        y = height - 60.0
        for line in lines:
            c.drawString(left, y, line)
            y -= line_h
        c.showPage()

    # Page 1 -- A/R Aging (synthetic filler rows).
    draw_page([
        "A/R Aging",
        "",
        "Account                     Balance",
        "Test Rewards Account         100.00",
        "Sample Direct Bill Co        250.00",
        "Mock City Ledger             (75.00)",
    ])

    # Page 2 -- Hotel Journal Summary (same synthetic rows as the fixture).
    journal_lines = [
        "Hotel Journal Summary",
        "",
        f"Property Name: {PROPERTY_NAME}",
        f"Business Date: {BUSINESS_DATE} Property Code: {PROPERTY_CODE}",
        "",
    ]
    for label, code, amounts in JOURNAL_ROWS:
        prefix = f"{label} {code}" if code is not None else label
        journal_lines.append(f"{prefix:<24} " + " ".join(amounts))
    draw_page(journal_lines)

    # Page 3 -- Hotel Statistics (same synthetic rows as the fixture).
    stats_lines = [
        "Hotel Statistics",
        "",
        f"Property Name: {PROPERTY_NAME}",
        f"Business Date: {BUSINESS_DATE} Property Code: {PROPERTY_CODE}",
        "",
        f"{'':<32} " + " ".join(STATS_ANCHORS),
    ]
    for label, values in STATISTICS_ROWS:
        stats_lines.append(f"{label:<32} " + " ".join(values))
    draw_page(stats_lines)

    c.save()


def main() -> None:
    write_json(JOURNAL_FIXTURE, build_journal_words())
    write_json(STATISTICS_FIXTURE, build_statistics_words())
    build_pack_pdf(PACK_PDF)
    print(f"wrote {JOURNAL_FIXTURE.relative_to(REPO_ROOT)}")
    print(f"wrote {STATISTICS_FIXTURE.relative_to(REPO_ROOT)}")
    print(f"wrote {PACK_PDF.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
