#!/usr/bin/env python3
"""Generate the synthetic HotelKey fixtures. Run: uv run python scripts/gen_hotelkey_mock_fixtures.py

Generates the fixture inputs the HotelKey tests in
docs/plans/2026-09-20-oh22-hotelkey.md read. Nothing here comes from a vendor
export except LAYOUT: column x-positions, row pitch, which rows wrap, where the
stamps sit. Names, codes and figures are invented and internally consistent
(section totals equal their rows), so a footing check on those totals can be
exercised without ever committing a sample.

Outputs:
  docs/reference/samples/HotelKey - Hotel Statistics (mock).pdf  (reportlab)
  tests/fixtures/hotelkey_hotel_statistics_words.json            (extract_words of that PDF)
  tests/fixtures/hotelkey/{Settlement By Payment Type,All Payments,AR Invoice Aging}.xlsx (openpyxl)

After drawing the PDF, ``check_statistics_geometry`` re-reads it with
``extract_pages`` + ``cluster_rows`` and asserts the layout properties listed
in docs/plans/2026-09-20-oh22-hotelkey.md Task 3 Step 2 (each value nearest
its own header token, ``%`` a separate token, wrapped label lines free of
numbers, a bare header row opening page 3, the footers, the header tokens at
their measured x0s). The generator fails rather than writing a fixture that
has drifted from that shape.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, time
from pathlib import Path

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from usali.adaptors.pdf import Word, cluster_rows, extract_pages, extract_words

REPO_ROOT = Path(__file__).resolve().parent.parent

PROPERTY_NAME = "Lakeside Test Lodge"
PROPERTY_CODE = "HKTEST"
REPORT_DATE = "Aug 13, 2026"
RUN_DATE = "Aug 14 2026"
RUN_TIME = "09:10:11 AM"
USER = "Sample TESTUSER"

PDF_OUT = REPO_ROOT / "docs" / "reference" / "samples" / "HotelKey - Hotel Statistics (mock).pdf"
WORDS_OUT = REPO_ROOT / "tests" / "fixtures" / "hotelkey_hotel_statistics_words.json"
XLSX_DIR = REPO_ROOT / "tests" / "fixtures" / "hotelkey"

# --- statistics PDF geometry (points, measured 2026-09-20) ---------------------
# A4. The running footer sits at top 800.5 / 810.0, which only exists on an
# 841.89pt page.
PAGE_W, PAGE_H = 595.2756, 841.8898
HEADER_TOKENS = [("Actual", 134.0), ("Today", 160.0), ("M-T-D", 240.0), ("LY-M-T-D", 326.0),
                 ("Y-T-D", 426.0), ("LY-T-D", 516.0)]
# The five period tokens; Task 3 Step 2 requires every value to be nearest its own.
PERIOD_ANCHORS = [x0 for tok, x0 in HEADER_TOKENS if tok != "Actual"]
# Measured on the vendor sample 2026-09-20. Nothing is drawn from these; the
# geometry check compares the drawn header tokens against them.
MEASURED_PERIOD_X0 = (160.0, 240.0, 326.0, 426.0, 516.0)
# Values are centred in their column (a column's x0s wander with the width of
# the number; its centres do not). A "%" follows a space inside the same cell.
VALUE_CENTER = [159.5, 251.7, 344.0, 436.0, 529.0]
HEADING_X0 = 24.0
# Labels, "Description" and the sub-section prefixes are all centred here.
LABEL_CENTER = 66.0
ROW_PITCH = 13.7
CONT_PITCH = 11.5
HEADING_PITCH = 15.4
BLOCK_GAP = 13.8      # extra space between a Totals row and the next sub-section header
SECTION_GAP = 34.0    # extra space after a section's last row
FONT = ("Helvetica", 8)
FOOTER_PAGE_TOP = 800.5
FOOTER_TITLE_TOP = 810.0

# (heading, [(sub_prefix or None, [ (label_lines, values5) ... ]) ...])
# values5: five strings or None (sparse); a "%" suffix is drawn as its own token.
Row = tuple[list[str], list[str | None]]
Block = tuple[str | None, list[Row]]
Section = tuple[str, list[Block]]

STATISTICS_PAGES: list[list[Section]] = [
    [  # page 1
        ("Room Statistics", [(None, [
            (["Total Rooms"], ["80", "2,480", "2,480", "18,160", "18,160"]),
            (["Clean"], ["70", "2,200", "2,150", "16,000", "15,900"]),
            (["Dirty"], ["6", "200", "250", "1,600", "1,700"]),
            (["Out Of Order"], ["4", "80", "80", "560", "560"]),
            (["Rooms Available To", "Sell"], ["76", "2,400", "2,400", "17,600", "17,600"]),
            (["Same Day Checkouts"], ["1", "20", "18", "150", "140"]),
            (["Stay Overs"], ["22", "700", "690", "5,100", "5,000"]),
            (["House Rooms"], ["1", "20", "15", "150", "140"]),
            (["Room Sold"], ["40", "1,240", "1,200", "9,080", "9,000"]),
            (["Rooms Sold Excluding", "Comp House Use", "Rooms"], ["37", "1,180", "1,150", "8,700", "8,650"]),
        ])]),
        ("Performance Statistics", [(None, [
            (["Occupancy Including", "Out of Order Rooms,", "Comp, House Use", "Rooms"],
             ["50.00%", "51.61%", "50.00%", "50.00%", "49.56%"]),
            (["Occupancy Excluding", "Out of Order Rooms,", "Comp, House Use", "Rooms"],
             ["48.68%", "49.17%", "47.92%", "49.43%", "49.15%"]),
            (["Occupancy Including", "Out of Order Rooms", "and Excluding Comp"],
             ["47.50%", "48.39%", "47.08%", "47.91%", "47.60%"]),
            (["Occupancy Excluding", "Out of Order Rooms", "and Including Comp,", "House Use Rooms"],
             ["52.63%", "51.67%", "50.00%", "51.59%", "51.14%"]),
        ])]),
        ("Revenue Performance", [(None, [
            (["ADR Including Comp", "House Use Rooms"], ["125.00", "126.10", "124.00", "125.50", "124.80"]),
            (["ADR Excluding Comp", "House Use Rooms"], ["135.14", "132.52", "129.39", "131.03", "129.83"]),
            (["RevPar With Out Of", "Order Rooms"], ["62.50", "63.05", "62.00", "62.75", "62.40"]),
            (["RevPAR"], ["65.79", "65.15", "64.58", "64.77", "64.36"]),
        ])]),
    ],
    [  # page 2
        ("Revenue Statistics", [
            ("Room Revenue", [
                (["Taxable Room", "Revenue"], ["4,800.00", "150,000.00", "0.00", "1,120,000.00", "0.00"]),
                (["Exempt Room", "Revenue"], ["200.00", "6,000.00", "0.00", "45,000.00", "0.00"]),
                (["Totals"], ["5,000.00", "156,000.00", "0.00", "1,165,000.00", "0.00"]),
            ]),
            ("Misc Revenue", [
                (["LATE CANCEL ROOM", "REVENUE"], ["100.00", "300.00", "0.00", "2,400.00", "0.00"]),
                (["SUNDRY - FOOD 1"], ["0.00", "50.00", "0.00", "400.00", "0.00"]),
                (["DAMAGE FEE"], ["25.00", "75.00", "0.00", "600.00", "0.00"]),
                (["DIFFERENCE DROP"], ["0.00", "0.00", "0.00", "10.00", "0.00"]),
                (["OTHER REVENUE"], ["0.00", "5.00", "0.00", "40.00", "0.00"]),
                (["Totals"], ["125.00", "430.00", "0.00", "3,450.00", "0.00"]),
                (["Totals"], ["5,125.00", "156,430.00", "0.00", "1,168,450.00", "0.00"]),  # grand total
            ]),
        ]),
        ("Taxes", [(None, [
            (["TAXABLE CITY TAX"], ["336.00", "10,500.00", "0.00", "78,400.00", "0.00"]),
            (["EXEMPTED CITY TAX"], ["-14.00", "-420.00", "0.00", "-3,150.00", "0.00"]),
            (["TAXABLE STATE TAX"], ["288.00", "9,000.00", "0.00", "67,200.00", "0.00"]),
            (["EXEMPTED STATE", "TAX"], ["-12.00", "-360.00", "0.00", "-2,700.00", "0.00"]),
            (["Totals"], ["598.00", "18,720.00", "0.00", "139,750.00", "0.00"]),
        ])]),
        ("Payments", [(None, [
            (["CASH"], ["100.00", "3,000.00", "0.00", "22,000.00", "0.00"]),
            (["AMEX"], ["500.00", "15,000.00", "0.00", "110,000.00", "0.00"]),
            (["MASTER"], ["2,000.00", "60,000.00", "0.00", "450,000.00", "0.00"]),
            (["VISA"], ["2,900.00", "87,000.00", "0.00", "650,000.00", "0.00"]),
            (["BILL TO COMPANY"], ["0.00", "0.00", "0.00", "1,000.00", "0.00"]),
            (["Totals"], ["5,500.00", "165,000.00", "0.00", "1,233,000.00", "0.00"]),
        ])]),
        # Like the real export: the heading and header row land at the foot of
        # the page and the header row repeats at the top of the next one.
        ("Guest Statistics", [(None, [])]),
    ],
    [  # page 3 -- starts with a bare header row (no heading), then rows
        ("", [(None, [
            (["Adults"], ["45", "1,400", "0", "10,200", "0"]),
            (["Children"], ["3", "60", "0", "400", "0"]),
            (["Total Guests"], ["48", "1,460", "0", "10,600", "0"]),
            (["Arrivals"], ["15", "480", "0", "3,500", "0"]),
            (["Departures"], ["14", "470", "0", "3,480", "0"]),
        ])]),
        ("Today's Activity", [(None, [
            (["New Booking"], ["30", "900", None, "6,600", "0"]),
            (["No Shows"], ["2", "30", None, "200", "0"]),
            (["Cancellation for", "Today's Arrival"], ["1", "12", None, "90", "0"]),
        ])]),
        ("Forecast Guest Statistics", [(None, [
            (["Tomorrows Arrivals"], ["12", None, None, None, None]),
            (["Guest Count For", "Tomorrows Arrivals"], ["20", None, None, None, None]),
        ])]),
    ],
]


def build_statistics_pdf(path: Path) -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
    c.setFont(*FONT)
    # The measured "top" is the glyph box's top edge; drawString places the
    # baseline, which sits (size + descent) below it. getDescent is negative.
    ascent = FONT[1] + pdfmetrics.getDescent(*FONT)

    def text(x: float, top: float, s: str) -> None:
        c.drawString(x, PAGE_H - top - ascent, s)

    def centred(x: float, top: float, s: str) -> None:
        c.drawCentredString(x, PAGE_H - top - ascent, s)

    def header_row(top: float, prefix: str) -> None:
        centred(LABEL_CENTER, top, prefix)
        for tok, x0 in HEADER_TOKENS:
            text(x0, top, tok)

    def value_row(top: float, lines: list[str], values: list[str | None]) -> float:
        centred(LABEL_CENTER, top, lines[0])
        for value, x in zip(values, VALUE_CENTER):
            if value is None:
                continue
            # A percent is "<number> %": the space makes "%" its own token.
            centred(x, top, value[:-1] + " %" if value.endswith("%") else value)
        for extra in lines[1:]:
            top += CONT_PITCH
            centred(LABEL_CENTER, top, extra)
        return top + ROW_PITCH

    def footer(page_no: int) -> None:
        text(536.0, FOOTER_PAGE_TOP, f"Page{page_no}")
        # "/ 3" as one string: the space keeps "/" and the page count apart.
        text(565.0, FOOTER_PAGE_TOP, f"/ {len(STATISTICS_PAGES)}")
        text(HEADING_X0, FOOTER_TITLE_TOP, "Hotel Statistics")

    for page_no, sections in enumerate(STATISTICS_PAGES, start=1):
        c.setFont(*FONT)
        top = 23.4
        if page_no == 1:
            text(104.0, top, f"{PROPERTY_NAME}, TX")
            text(486.0, top, f"Date: {REPORT_DATE}")
            text(104.0, 37.7, PROPERTY_CODE)
            text(432.0, 37.7, f"Report Run Date: {RUN_DATE}")
            text(430.0, 52.0, f"Report Run Time: {RUN_TIME}")
            text(39.0, 68.4, "MOCK DATA")
            text(460.0, 68.4, f"User: {USER}")
            text(47.0, 78.4, PROPERTY_CODE)
            text(225.0, 98.2, "Hotel Statistics")
            top = 156.3
        for heading, blocks in sections:
            if heading:
                text(HEADING_X0, top, heading)
                top += HEADING_PITCH
            for i, (prefix, rows) in enumerate(blocks):
                if i:
                    top += BLOCK_GAP
                header_row(top, prefix or "Description")
                top += ROW_PITCH
                for lines, values in rows:
                    top = value_row(top, lines, values)
            top += SECTION_GAP
        footer(page_no)
        c.showPage()
    c.save()


_NUM_RE = re.compile(r"^-?[\d,]+(\.\d+)?$")


def _rows(page: list[Word]) -> list[list[Word]]:
    return [sorted(row, key=lambda w: w.x0) for row in cluster_rows(page)]


def check_statistics_geometry(pdf: Path) -> None:
    """Assert the Task 3 Step 2 layout properties on the drawn PDF."""
    actual_x0 = dict(HEADER_TOKENS)["Actual"]
    label_limit = actual_x0 - 10.0
    pages = extract_pages(pdf)
    assert len(pages) == len(STATISTICS_PAGES), len(pages)
    expected_rows = [
        (lines, values)
        for page in STATISTICS_PAGES
        for _, blocks in page
        for _, rows in blocks
        for lines, values in rows
    ]
    seen: list[tuple[list[str], list[str | None]]] = []
    armed = False  # rows before the first header row are the stamp block
    for page_no, page in enumerate(pages, start=1):
        rows = _rows(page)
        texts = [[w.text for w in row] for row in rows]
        assert texts[-2][0].startswith("Page") and texts[-2][1:] == ["/", str(len(pages))], texts[-2]
        assert all(w.x0 > label_limit for w in rows[-2]), rows[-2]
        assert texts[-1] == ["Hotel", "Statistics"], texts[-1]
        if page_no == 3:
            assert texts[0][0] == "Description" and "Today" in texts[0], texts[0]
        pending: tuple[list[str], list[str | None]] | None = None
        prev_valueless = False
        for row in rows[:-2]:
            by_text = {w.text: w.x0 for w in row}
            if "Today" in by_text:
                # header rows carry the five period tokens at the measured x0s
                period_tokens = [tok for tok, _ in HEADER_TOKENS if tok != "Actual"]
                for tok, x0 in zip(period_tokens, MEASURED_PERIOD_X0):
                    assert abs(by_text[tok] - x0) <= 1.0, (tok, by_text.get(tok), x0)
                if pending is not None and prev_valueless:
                    pending[0].pop()  # the row above a header is a section heading
                armed = True
                prev_valueless = False
                continue
            if not armed:
                continue
            assert not any(w.text.endswith("%") and w.text != "%" for w in row), (
                "a percent merged with its number", row)
            nums = [w for w in row if _NUM_RE.match(w.text) and w.x0 > label_limit]
            label = [w.text for w in row if w.x0 <= label_limit]
            if not nums:
                if pending is not None and label:
                    pending[0].append(" ".join(label))
                prev_valueless = True
                continue
            prev_valueless = False
            if pending is not None:
                seen.append(pending)
            pending = ([" ".join(label)], [None] * 5)
            for w in nums:
                col = min(range(5), key=lambda i: abs(w.x0 - PERIOD_ANCHORS[i]))
                assert pending[1][col] is None, ("two values nearest one anchor", row)
                nxt = next((v for v in row if v.x0 > w.x0), None)
                pct = nxt is not None and nxt.text == "%"
                pending[1][col] = w.text + ("%" if pct else "")
                if col == 0:
                    assert w.x0 > label_limit, ("DAY value left of the boundary", w)
        if pending is not None:
            seen.append(pending)
    assert seen == expected_rows, "drawn rows do not read back as authored"


def write_statistics_words(pdf: Path, out: Path) -> None:
    words = extract_words(pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps([{"text": w.text, "x0": round(w.x0, 2), "top": round(w.top, 2)} for w in words],
                   indent=1) + "\n"
    )


# --- XLSX -----------------------------------------------------------------------
def _sheet(title_cell: str, right_col: str, date_line: str) -> tuple[Workbook, Worksheet]:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Report"
    ws["A1"] = PROPERTY_NAME
    ws["A2"] = PROPERTY_CODE
    ws[f"{right_col}1"] = date_line
    ws[f"{right_col}2"] = f"Report Run Date: {RUN_DATE}"
    ws[f"{right_col}3"] = f"Report Run Time: {RUN_TIME}"
    ws[f"{right_col}4"] = f"User: {USER}"
    ws["A8"] = title_cell
    return wb, ws


def build_settlement(path: Path) -> None:
    wb, ws = _sheet("Settlement By Payment Type", "N", "Date Range: Aug 12, 2026 - Aug 13, 2026")
    ws["B11"] = "Details"
    headers = ["Account Category", "Date", "Time", "Transaction Number", "Folio Number",
               "Guest Name", "First Name", "Last Name", "Account Name", "Room Number",
               "Payment Type", "Payment Description", "Amount", "Username", "Remarks"]
    for i, h in enumerate(headers):
        ws.cell(row=12, column=2 + i, value=h)
    details = [
        (1, datetime(2026, 8, 12), time(15, 12, 34), "10000001", "000101", "TESTGUEST ALPHA", "ALPHA", "TESTGUEST", "201", "MASTER", "1111", 300.00),
        (2, datetime(2026, 8, 13), time(12, 5, 34), "10000002", "000102", "TESTGUEST BRAVO", "BRAVO", "TESTGUEST", "202", "MASTER", "2222", 150.50),
        (3, datetime(2026, 8, 13), time(11, 32, 35), "10000003", "000103", "TESTGUEST CHARLIE", "CHARLIE", "TESTGUEST", "203", "MASTER", "3333", 249.50),
        # subtotal row: ordinal + amount only, as the export prints it
        (4, None, None, None, None, None, None, None, None, None, None, 700.00),
        (4, datetime(2026, 8, 13), time(21, 47, 48), "10000004", "000104", "TESTGUEST DELTA", "DELTA", "TESTGUEST", "301", "VISA", "4444", 120.25),
        (5, datetime(2026, 8, 13), time(7, 0, 8), "10000005", "000105", "TESTGUEST ECHO", "ECHO", "TESTGUEST", "302", "VISA", "5555", 79.75),
        (5, None, None, None, None, None, None, None, None, None, None, 200.00),
    ]
    r = 13
    for ordinal, d, t, txn, folio, guest, first, last, room, ptype, desc, amount in details:
        ws.cell(row=r, column=1, value=ordinal)
        if d is not None:
            ws.cell(row=r, column=2, value="Reservation")
            ws.cell(row=r, column=3, value=d)
            ws.cell(row=r, column=4, value=t)
            ws.cell(row=r, column=5, value=txn)
            ws.cell(row=r, column=6, value=folio)
            ws.cell(row=r, column=7, value=guest)
            ws.cell(row=r, column=8, value=first)
            ws.cell(row=r, column=9, value=last)
            ws.cell(row=r, column=11, value=room)
            ws.cell(row=r, column=12, value=ptype)
            ws.cell(row=r, column=13, value=desc)
            ws.cell(row=r, column=15, value="testusr01")
        ws.cell(row=r, column=14, value=amount)
        r += 1
    ws.cell(row=r, column=14, value=900.00)  # grand total, unnumbered
    ws["B23"] = "Summary"
    for i, h in enumerate(["Payment Type", "Amount", "Count"]):
        ws.cell(row=24, column=2 + i, value=h)
    ws["A25"], ws["B25"], ws["C25"], ws["D25"] = 1, "MASTER", 700.00, 3
    ws["A26"], ws["B26"], ws["C26"], ws["D26"] = 2, "VISA", 200.00, 2
    ws["C27"], ws["D27"] = 900.00, 5
    ws["A30"] = "END OF REPORT"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def build_all_payments(path: Path) -> None:
    wb, ws = _sheet("All Payments", "I", "Date Range: Aug 13, 2026 - Aug 13, 2026")
    ws["B14"] = "All Payments - Payment Type"
    ws["B15"], ws["C15"] = "Payment Type", "Amount"
    ws["A16"], ws["B16"], ws["C16"] = 1, "MASTER", 700.00
    ws["A17"], ws["B17"], ws["C17"] = 2, "VISA", 200.00
    ws["C18"] = 900.00
    ws["A20"] = "END OF REPORT"
    wb.save(path)


AR_COLS = ["Payment On Account", "Current", "31 to 60", "61 to 90", "91 to 120", "121 to 150",
           "Over 150", "Total"]
AR_TOTAL = [0, 1000.00, 2000.00, 500.00, 0, 0, -100.00, 3400.00]


def build_ar_aging(path: Path) -> None:
    wb, ws = _sheet("AR Invoice Aging", "I", "Date: Aug 13, 2026")
    r = 14
    sections = [
        ("Transaction Type", [("Payment", [0] * 8), ("Charge", AR_TOTAL)]),
        ("Date", [("2026-08-01", [0, 1000.00, 0, 0, 0, 0, 0, 1000.00]),
                  ("2026-07-01", [0, 0, 2000.00, 0, 0, 0, 0, 2000.00]),
                  ("2026-06-01", [0, 0, 0, 500.00, 0, 0, -100.00, 400.00])]),
        ("Company Name", [("TEST CORP TRAVEL", [0, 1000.00, 2000.00, 0, 0, 0, 0, 3000.00]),
                          ("DEMO PARTNER LLC", [0, 0, 0, 500.00, 0, 0, -100.00, 400.00])]),
        ("Transaction Status", [("CLOSED", [0, 0, 0, 0, 0, 0, -100.00, -100.00]),
                                ("CHECKED OUT", [0, 1000.00, 2000.00, 500.00, 0, 0, 0, 3500.00])]),
    ]
    for name, rows in sections:
        ws.cell(row=r, column=2, value=f"AR Aging Details - {name}")
        r += 1
        ws.cell(row=r, column=2, value=name)
        for i, col in enumerate(AR_COLS):
            ws.cell(row=r, column=3 + i, value=col)
        r += 1
        for ordinal, (label, values) in enumerate(rows, start=1):
            ws.cell(row=r, column=1, value=ordinal)
            ws.cell(row=r, column=2, value=label)
            for i, v in enumerate(values):
                ws.cell(row=r, column=3 + i, value=v)
            r += 1
        for i, v in enumerate(AR_TOTAL):
            ws.cell(row=r, column=3 + i, value=v)
        r += 2
    ws.cell(row=r, column=1, value="END OF REPORT")
    wb.save(path)


def main() -> None:
    build_statistics_pdf(PDF_OUT)
    check_statistics_geometry(PDF_OUT)
    write_statistics_words(PDF_OUT, WORDS_OUT)
    build_settlement(XLSX_DIR / "Settlement By Payment Type.xlsx")
    build_all_payments(XLSX_DIR / "All Payments.xlsx")
    build_ar_aging(XLSX_DIR / "AR Invoice Aging.xlsx")
    for out in (PDF_OUT, WORDS_OUT, XLSX_DIR):
        print(f"wrote {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
