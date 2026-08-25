#!/usr/bin/env python3
"""Generate the two SYNTHETIC sample reports offered on the /try front door.

EVERYTHING here is invented. No figure, guest, property or transaction code
from any real export appears in this file or its outputs.

That is the entire point of this script. `docs/reference/samples/*.pdf` are REAL
exports carrying REAL production numbers -- `scripts/cloud/job.sh` says so, and
it is why the cloud seed runs with `--synthetic-year`. They can never be the
thing a stranger downloads from the public preview page. These two can, because
they only mirror the LAYOUT GEOMETRY a real export uses: the column positions,
the section rows, the code column, the header tokens each parser anchors on.

Two properties, so nobody mistakes them for one hotel's books:

  Seabright Harbor Inn  -- Opera trial balance
  Cedar Point Lodge     -- AutoClerk transaction summary

Both are built to survive the real pipeline, not a mock of it:
`tests/test_preview_samples.py` runs each PDF through `detect_report_signature`
and the registered preview adapter and asserts an `ok` payload. If a parser
changes shape, the samples fail in CI rather than silently degrading into an
"we couldn't read that file" on the page we invited people to.

Run with::

    uv run python scripts/gen_preview_samples.py
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "frontend" / "public" / "samples"

OPERA_PDF = OUT_DIR / "opera-trial-balance-sample.pdf"
AUTOCLERK_PDF = OUT_DIR / "autoclerk-transaction-summary-sample.pdf"

# --- Opera trial balance -----------------------------------------------------

OPERA_PROPERTY = "Seabright Harbor Inn"
# `opera_trial_balance.extract_business_date` takes the first MM-DD-YY token.
OPERA_DATE = "06-21-26"

# (section, code, description, amount). The section strings must match
# `opera_trial_balance.SECTIONS` EXACTLY and print alone on their own row.
#
# Codes are drawn from mapping/opera.yaml so the preview shows real USALI lines
# -- except 5210, which is deliberately UNMAPPED. A sample where everything maps
# would advertise a coverage we do not have; the honest picture is that a first
# look at a new property leaves a handful of codes to confirm, and the page says
# so. Do not "fix" 5210 by adding it to the mapping.
OPERA_ROWS: list[tuple[str, str, str, str]] = [
    ("Revenue", "1000", "Room Revenue", "14,820.00"),
    ("Revenue", "5106", "Gift Shop Taxable", "312.50"),
    ("Revenue", "5105", "Parking", "485.00"),
    ("Revenue", "5210", "Pet Fee", "150.00"),
    ("Non Revenue", "7100", "Transient Occupancy Tax", "1,482.00"),
    ("Non Revenue", "7101", "CCFD", "592.80"),
    ("Non Revenue", "7102", "CA Tourism Assessment", "28.90"),
    ("Non Revenue", "7104", "Sales Tax", "74.72"),
    ("Non Revenue", "5007", "Hotel BID Fee", "44.46"),
    ("Payment", "9004", "Visa", "-9,850.00"),
    ("Payment", "9005", "MasterCard", "-3,420.00"),
    ("Payment", "9003", "American Express", "-1,180.00"),
    ("Payment", "9007", "Discover", "-640.00"),
    ("Payment", "9002", "City Ledger", "-2,900.38"),
]

# --- AutoClerk transaction summary -------------------------------------------

AUTOCLERK_PROPERTY = "Cedar Point Lodge"
# `autoclerk_transaction_summary.extract_business_date` takes the first
# MM/DD/YYYY token.
AUTOCLERK_DATE = "06/21/2026"

# (category, type, TODAY, MTD, YTD). Category names slug to the CATEGORY half of
# the codes in mapping/autoclerk.yaml (`_slug`: upper-case, non-alphanumerics to
# underscores), and type names to the other half -- "Room"/"Room Rent" is the
# code ROOM|ROOM_RENT. Type names must contain NO DIGITS: the parser finds a
# row's value by scanning for the first token that looks numeric, so a digit in
# the name would be read as the amount.
AUTOCLERK_ROWS: list[tuple[str, str, str, str, str]] = [
    ("Room", "Room Rent", "9,640.00", "182,410.00", "1,043,880.00"),
    ("Room", "Late Check Out", "75.00", "1,350.00", "8,215.00"),
    ("Tax", "Occupancy Tax", "964.00", "18,241.00", "104,388.00"),
    ("Tax", "County Tax", "192.80", "3,648.20", "20,877.60"),
    ("Misc", "Pet Fee", "100.00", "1,850.00", "9,400.00"),
    ("Parking", "Parking Fees", "210.00", "4,120.00", "23,650.00"),
    ("HIE Market Sell", "Water", "18.00", "396.00", "2,214.00"),
    ("HIE Market Sell", "Soda", "24.00", "512.00", "2,868.00"),
    ("Credit Cards", "Visa", "-6,800.00", "-129,400.00", "-742,300.00"),
    ("Credit Cards", "MasterCard", "-2,450.00", "-46,900.00", "-268,150.00"),
    ("Credit Cards", "American Express", "-880.00", "-16,300.00", "-93,700.00"),
    ("Cash", "Cash", "-520.00", "-9,880.00", "-56,200.00"),
    ("Accounts", "Direct Bill", "-573.80", "-10,047.20", "-55,142.60"),
]


def _amount(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _grand_total_today() -> Decimal:
    """The TODAY column's exact sum.

    `parse_transaction_summary` REFUSES a report whose rows do not add up to its
    GRAND TOTAL -- it would rather raise than hand back a parse it could not
    verify. So this is computed, never typed: a hand-written total that drifted
    by a cent would turn the sample into an "unreadable file" on the front door.
    """
    return sum((_amount(r[2]) for r in AUTOCLERK_ROWS), Decimal("0"))


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _draw(canvas, page_size, lines: list[str], *, left: float = 54.0) -> None:
    """Render one monospace page. Courier gives every character the same width,
    so space-padded fields land in true pixel columns -- which is what the
    parsers key on (Opera's leftmost-token code column, AutoClerk's far-left
    category column at x0 < 120)."""
    canvas.setFont("Courier", 9)
    y = page_size[1] - 54.0
    for line in lines:
        canvas.drawString(left, y, line)
        y -= 13.0
    canvas.showPage()


def build_opera_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as canvas_mod

    code_w, desc_w = 10, 46
    lines = [
        "SEABRIGHT HARBOR INN",
        "Trial Balance",
        f"Business Date: {OPERA_DATE}                 Property: SBHI",
        "",
        "Code".ljust(code_w) + "Description".ljust(desc_w) + "Amount",
        "-" * 74,
    ]
    current: str | None = None
    for section, code, desc, amount in OPERA_ROWS:
        if section != current:
            # A section row must join to EXACTLY "Revenue" / "Non Revenue" /
            # "Payment" -- anything else on the line and the parser stops
            # treating it as a section marker.
            lines.extend(["", section])
            current = section
        lines.append(code.ljust(code_w) + desc.ljust(desc_w) + amount.rjust(14))

    # The roll-up Opera prints below the transaction sections. It carries no
    # leading 3-5 digit code, so the parser ignores it; it is here because a real
    # trial balance has one and the sample should look like the thing it stands
    # in for.
    charges = sum(
        (_amount(a) for s, _, _, a in OPERA_ROWS if s != "Payment"), Decimal("0")
    )
    payments = sum(
        (_amount(a) for s, _, _, a in OPERA_ROWS if s == "Payment"), Decimal("0")
    )
    lines.extend([
        "",
        "Transaction Total Today".ljust(code_w + desc_w) + _money(charges + payments).rjust(14),
        "",
        "Guest Ledger",
        "Guest Ledger is in balance",
    ])

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas_mod.Canvas(str(path), pagesize=letter)
    _draw(c, letter, lines)
    c.save()


def build_autoclerk_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas as canvas_mod

    name_w, col_w = 30, 16
    lines = [
        "CEDAR POINT LODGE",
        "Transaction Summary",
        f"Business Date: {AUTOCLERK_DATE}",
        "",
        # The parser starts reading at the row carrying BOTH "Category" and
        # "TODAY", and re-skips it on every page.
        "Category".ljust(name_w) + "TODAY".rjust(col_w) + "MTD".rjust(col_w) + "YTD".rjust(col_w),
        "-" * 78,
    ]
    current: str | None = None
    for category, name, today, mtd, ytd in AUTOCLERK_ROWS:
        if category != current:
            # A category header carries NO amount and sits in the far-left
            # column; both are load-bearing for how it is recognised.
            lines.extend(["", category])
            current = category
        lines.append(
            ("  " + name).ljust(name_w)
            + today.rjust(col_w) + mtd.rjust(col_w) + ytd.rjust(col_w)
        )

    total = _grand_total_today()
    lines.extend([
        "",
        "GRAND TOTAL".ljust(name_w) + _money(total).rjust(col_w),
    ])

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas_mod.Canvas(str(path), pagesize=letter)
    _draw(c, letter, lines)
    c.save()


def main() -> None:
    build_opera_pdf(OPERA_PDF)
    build_autoclerk_pdf(AUTOCLERK_PDF)
    for p in (OPERA_PDF, AUTOCLERK_PDF):
        print(f"wrote {p.relative_to(REPO_ROOT)} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
