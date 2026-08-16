import io
from dataclasses import dataclass
from pathlib import Path

import pdfplumber


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    top: float


def extract_words_from_bytes(data: bytes) -> list[Word]:
    words: list[Word] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            page_offset = i * (page.height + 1000)
            for w in page.extract_words():
                words.append(
                    Word(text=w["text"], x0=float(w["x0"]), top=float(w["top"]) + page_offset)
                )
    return words


def extract_words(pdf_path: str | Path) -> list[Word]:
    return extract_words_from_bytes(Path(pdf_path).read_bytes())


def cluster_rows(words: list[Word], y_tol: float = 3.0) -> list[list[Word]]:
    # Group words into visual rows. Compare each candidate to the row's most-recently-added
    # word (largest top so far) so cumulative vertical drift on skewed exports chains
    # together instead of splitting a real row.
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        for row in rows:
            if abs(row[-1].top - w.top) <= y_tol:
                row.append(w)
                break
        else:
            rows.append([w])
    return rows
