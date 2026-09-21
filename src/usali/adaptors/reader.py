"""Content-dispatching reader: the one place a file's format is decided.

Magic bytes, never the suffix -- a renamed file must not change how it is
parsed, so a caller that checks magic before writing to disk and this reader
agree on what a format is. ``read_words`` is the one entry point for reading
a report file; a caller that needs Words from a path or bytes comes here
rather than choosing a reader itself.
"""

from pathlib import Path

from usali.adaptors.magic import ACCEPTED_FORMATS, is_pdf, is_xlsx
from usali.adaptors.pdf import Word, extract_words_from_bytes
from usali.adaptors.xlsx import extract_words_from_xlsx_bytes

# Re-exported from the leaf module `usali.adaptors.magic`, which imports
# nothing, so every existing `from usali.adaptors.reader import is_pdf` keeps
# working while a caller that must not pull in the parsers can import the
# predicates alone.
__all__ = [
    "ACCEPTED_FORMATS", "Word", "is_pdf", "is_xlsx",
    "read_words", "read_words_from_bytes",
]


def read_words_from_bytes(data: bytes) -> list[Word]:
    if is_pdf(data):
        return extract_words_from_bytes(data)
    if is_xlsx(data):
        return extract_words_from_xlsx_bytes(data)
    raise ValueError(f"unsupported file format: expected {ACCEPTED_FORMATS} magic bytes")


def read_words(path: str | Path) -> list[Word]:
    return read_words_from_bytes(Path(path).read_bytes())
