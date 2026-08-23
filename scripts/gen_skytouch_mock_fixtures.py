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
# Statistics labels sit further left and tighter than the journal's, because the
# first value column is at x0 221.85 in a real export (vs 300 for the journal).
# A 5-token label like "ADR for Total Occupied Rooms" has to clear that column.
STATS_LABEL_X0 = 38.19
STATS_LABEL_STEP = 30.0
CODE_X0 = 250.0

# 7 amount columns for the Hotel Journal Summary.
JOURNAL_COLS = [300.0, 370.0, 440.0, 510.0, 580.0, 650.0, 720.0]

# 5 value columns for the Hotel Statistics report, and the REAL header geometry
# above them. These x0s are layout coordinates measured from a real Standard
# Audit Pack -- no figures, names or codes from it appear here.
#
# A real header is not one fixed phrase. Every section repeats it, in two
# variants that differ only by a "Current" prefix, at IDENTICAL column x0:
#
#   Room Statistics        <date>         PTD  Last Year PTD         YTD  Last YTD
#   Performance Statistics <date> Current PTD  Last Year PTD Current YTD  Last YTD
#
# Values sit under the LAST token of each group. The fixture emits the "Current"
# variant -- the harder of the two for a parser that keys off token positions.
STATS_COLS = [246.0, 313.0, 384.0, 450.0, 522.0]

# (label, transaction-code token, [7 amounts left-to-right]).
JOURNAL_ROWS: list[tuple[str, str | None, list[str]]] = [
    ("Cash", "(CA)", ["(500.00)", "0.00", "0.00", "(500.00)", "(500.00)", "0.00", "0.00"]),
    ("Room Charge", "(RM)", ["4000.00", "0.00", "0.00", "4000.00", "4000.00", "0.00", "0.00"]),
    ("State Tax", "(T1)", ["250.00", "0.00", "0.00", "250.00", "250.00", "0.00", "0.00"]),
    ("Visa Payment", "(VI)",
     ["(3750.00)", "0.00", "0.00", "(3750.00)", "(3750.00)", "0.00", "0.00"]),
    ("Today's Total:", None, ["0.00", "0.00", "0.00", "0.00", "0.00", "0.00", "0.00"]),
]

# Header tokens as a real export lays them out: (text, x0). ``None`` text is the
# business date, substituted at build time. The five columns are
# ACTUAL / PTD / LY_PTD / YTD / LY_YTD, anchored on the last token of each group
# (x0 221.85 / 317.61 / 388.10 / 459.67 / 531.65).
STATS_HEADER: list[tuple[str | None, float]] = [
    (None, 221.85),
    ("Current", 289.00), ("PTD", 317.61),
    ("Last", 352.69), ("Year", 370.04), ("PTD", 388.10),
    ("Current", 431.00), ("YTD", 459.67),
    ("Last", 514.30), ("YTD", 531.65),
]

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


def _label_words(
    label: str, top: float, x0: float = LABEL_X0, step: float = LABEL_STEP
) -> list[dict[str, object]]:
    """Split a label into tokens laid out left-to-right from `x0`, `step` apart."""
    words: list[dict[str, object]] = []
    for i, tok in enumerate(label.split(" ")):
        words.append(_word(tok, x0 + i * step, top))
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
    # Section + column header row, in the real multi-word shape.
    words.append(_word("Room", 38.19, 50.0))
    words.append(_word("Statistics", 61.25, 50.0))
    for text, x0 in STATS_HEADER:
        words.append(_word(text if text is not None else BUSINESS_DATE, x0, 50.0))
    top = 70.0
    for label, values in STATISTICS_ROWS:
        words.extend(_label_words(label, top, STATS_LABEL_X0, STATS_LABEL_STEP))
        for value, x0 in zip(values, STATS_COLS):
            words.append(_word(value, x0, top))
        top += 20.0
    return words


def write_json(path: Path, words: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(words, indent=1) + "\n")


def build_pack_pdf(path: Path) -> None:
    """Render a synthetic multi-page audit pack with reportlab (Courier)."""
    from reportlab.lib.pagesizes import landscape, letter
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

    def draw_lines_at(page_height: float, lines: list[str]) -> None:
        c.setFont("Courier", 10)
        y = page_height - 60.0
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

    # Page 2 -- Cancellation List. Carries no financial data we ingest; it is here
    # purely as a REGRESSION FIXTURE for issue #78. Four real SkyTouch reports print
    # a `Rate Plan` COLUMN, and the 120-word header window matched that against the
    # bare AutoClerk `RATE PLAN` signature, routing the section to the wrong
    # adapter. The mock pack could not express that before -- no section carried a
    # foreign column heading -- so the false positive was invisible until a real
    # multi-section pack was run through detection. Column names mirror the real
    # report's; every value is synthetic.
    draw_page([
        "Cancellation List",
        "",
        f"Property Name: {PROPERTY_NAME}",
        f"Business Date: {BUSINESS_DATE} Property Code: {PROPERTY_CODE}",
        "",
        "GUEST NAME        ARRIVAL     NIGHTS  RATE PLAN   GTD  SOURCE",
        "Test Guest One    06/22/2026       2  RACK        CC   Web",
        "Sample Guest Two  06/23/2026       1  CORP        CC   Phone",
    ])

    # Page 3 -- Hotel Journal Summary. Rendered in LANDSCAPE with FIXED-WIDTH monospace
    # columns so the "Postings" column header lands directly above its money column (the
    # header-anchored parser derives the Postings x0 from that header). Courier is
    # monospace, so equal character widths == equal pixel columns; the wide right-hand
    # columns (7 money cols) need the landscape width to fit. Fields are space-padded so
    # pdfplumber keeps each cell a distinct word.
    label_w, col_w = 30, 13
    headers = ["Postings", "Corrections", "Adjustments", "Totals",
               "GuestLedger", "ARLedger", "AdvDepLedger"]
    journal_lines = [
        "Hotel Journal Summary",
        "",
        f"Property Name: {PROPERTY_NAME}",
        f"Business Date: {BUSINESS_DATE} Property Code: {PROPERTY_CODE}",
        "",
        " " * label_w + "".join(h.ljust(col_w) for h in headers),
    ]
    for label, code, amounts in JOURNAL_ROWS:
        prefix = f"{label} {code}" if code is not None else label
        journal_lines.append(prefix.ljust(label_w) + "".join(a.ljust(col_w) for a in amounts))
    land = landscape(letter)  # (792, 612)
    c.setPageSize(land)
    draw_lines_at(land[1], journal_lines)

    # Page 4 -- Hotel Statistics. LANDSCAPE monospace, like page 3: now that this
    # section IS ingested, its column geometry matters. Each header group is
    # placed so its LAST token starts at its value column ("Current PTD" ->
    # "PTD" over the PTD values), which is how a real export aligns them.
    stats_label_w, stats_col_w = 30, 18
    header_groups = [
        (BUSINESS_DATE, ""),          # (last token, prefix)
        ("PTD", "Current "),
        ("PTD", "Last Year "),
        ("YTD", "Current "),
        ("YTD", "Last "),
    ]
    header = [" "] * (stats_label_w + stats_col_w * len(header_groups))
    for i, (last, prefix) in enumerate(header_groups):
        start = stats_label_w + i * stats_col_w - len(prefix)
        for k, ch in enumerate(prefix + last):
            header[start + k] = ch
    stats_lines = [
        "Hotel Statistics",
        "",
        f"Property Name: {PROPERTY_NAME}",
        f"Business Date: {BUSINESS_DATE} Property Code: {PROPERTY_CODE}",
        "",
        "Room Statistics".ljust(stats_label_w - 4) + "".join(header)[stats_label_w - 4 :],
    ]
    for label, values in STATISTICS_ROWS:
        stats_lines.append(
            label.ljust(stats_label_w) + "".join(v.ljust(stats_col_w) for v in values)
        )
    land = landscape(letter)
    c.setPageSize(land)
    c.setFont("Courier", 8)
    y = land[1] - 60.0
    for line in stats_lines:
        c.drawString(left, y, line)
        y -= line_h
    c.showPage()

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
