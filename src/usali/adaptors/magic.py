"""Format detection by magic bytes, and nothing else.

A LEAF module on purpose: it imports nothing, so a caller that only needs to
ask "is this a PDF or an XLSX?" — the webhook deciding whether a MIME part is
worth keeping — gets the answer without dragging pdfminer, openpyxl, numpy and
Pillow into the process. `usali.adaptors.reader` re-exports these three names,
so a caller that does go on to parse keeps importing from there.

Magic bytes, never the suffix: a renamed file must not change how it is
parsed. tests/test_intake.py::test_intake_imports_no_parser is what keeps this
module free of parser imports.
"""

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
ACCEPTED_FORMATS = "PDF or XLSX"


def is_pdf(data: bytes) -> bool:
    return data.startswith(_PDF_MAGIC)


def is_xlsx(data: bytes) -> bool:
    return data.startswith(_ZIP_MAGIC)
