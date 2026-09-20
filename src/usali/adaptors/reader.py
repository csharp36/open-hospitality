"""Content-dispatching reader: the one place a file's format is decided.

Magic bytes, never the suffix -- a renamed file must not change how it is
parsed, so a caller that checks magic before writing to disk and this reader
agree on what a format is. ``read_words`` is the one entry point for reading
a report file; a caller that needs Words from a path or bytes comes here
rather than choosing a reader itself.
"""

from pathlib import Path

from usali.adaptors.pdf import Word, extract_words_from_bytes
from usali.adaptors.xlsx import extract_words_from_xlsx_bytes

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
ACCEPTED_FORMATS = "PDF or XLSX"


def is_pdf(data: bytes) -> bool:
    return data.startswith(_PDF_MAGIC)


def is_xlsx(data: bytes) -> bool:
    return data.startswith(_ZIP_MAGIC)


def read_words_from_bytes(data: bytes) -> list[Word]:
    if is_pdf(data):
        return extract_words_from_bytes(data)
    if is_xlsx(data):
        return extract_words_from_xlsx_bytes(data)
    raise ValueError(f"unsupported file format: expected {ACCEPTED_FORMATS} magic bytes")


def read_words(path: str | Path) -> list[Word]:
    return read_words_from_bytes(Path(path).read_bytes())
