"""Read an XLSX workbook as the same ``list[Word]`` stream the PDF reader yields.

Every non-empty cell becomes one Word on a synthetic grid: ``x0`` is the
1-based column index times ``XLSX_COL_STEP`` and ``top`` the 1-based row index
times ``XLSX_ROW_STEP``. ``cluster_rows`` at its default tolerance then
recovers the sheet's rows exactly (tests/adaptors/test_xlsx.py::
test_cluster_rows_recovers_the_sheet_rows). A parser can recover the column
with ``round(x0 / XLSX_COL_STEP)``. A cell's text is its value rendered
exactly: integers as digits, floats through ``Decimal(repr(...))`` so
``361.63`` stays ``361.63``, midnight datetimes as ISO dates, other datetimes
and times as ISO.

Only the FIRST worksheet is read (tests/adaptors/test_xlsx.py::
test_only_the_first_worksheet_is_read): the four HotelKey sample exports read
on 2026-09-20 each carry one sheet, named ``Report``; a second sheet would be
a format change worth noticing rather than silently concatenating.
"""

import io
import zipfile
from datetime import date, datetime, time
from decimal import Decimal

import openpyxl

from usali.adaptors.pdf import Word

XLSX_COL_STEP = 100.0
XLSX_ROW_STEP = 10.0


def _cell_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            # openpyxl 3.1.5 _reader._cast_number returns int unless the raw
            # text has '.', 'E' or 'e' (read 2026-09-20), so a whole number
            # reaches here as int from its own files and as float from a
            # producer that writes a point. Either way an amount prints
            # without it. Pinned by
            # test_cell_text_renders_a_whole_number_float_without_a_point.
            return str(int(value))
        return format(Decimal(repr(value)), "f")
    if isinstance(value, datetime):
        if value.time() == time(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, str):
        text = value.strip()
        return text or None
    raise ValueError(f"unsupported cell value type {type(value).__name__}")


def extract_words_from_xlsx_bytes(data: bytes) -> list[Word]:
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    # SyntaxError is caught for its subclass xml.etree.ElementTree.ParseError,
    # raised on a corrupt sheet XML; catching the parent instead of importing
    # ElementTree here means a different XML parser backend raising a sibling
    # SyntaxError subclass is still caught.
    except (zipfile.BadZipFile, KeyError, ValueError, OSError, SyntaxError) as exc:
        raise ValueError(f"not an XLSX workbook: {exc}") from exc
    try:
        sheet = workbook.worksheets[0]
        words: list[Word] = []
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            for col_index, value in enumerate(row, start=1):
                text = _cell_text(value)
                if text is not None:
                    words.append(
                        Word(text=text, x0=col_index * XLSX_COL_STEP, top=row_index * XLSX_ROW_STEP)
                    )
        return words
    finally:
        workbook.close()
