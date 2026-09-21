import re
from dataclasses import dataclass, replace

from usali.adaptors.pdf import Word, cluster_rows
from usali.preview import PreviewPayload

_PAN_RUN = re.compile(r"\b\d[\d -]{11,21}\d\b")
_NAME_PAIR = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")
_DIGIT_CELL = re.compile(r"[\d -]+")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_pans(text: str) -> str:
    """Mask credit-card-shaped digit runs that pass the Luhn check -> '•••• last4'."""

    def _sub(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return f"•••• {digits[-4:]}"
        return m.group(0)

    return _PAN_RUN.sub(_sub, text)


def mask_names(text: str) -> str:
    """Mask 'Firstname Lastname' pairs. NOT applied to mapping-authored labels
    (which are curated, never PII) — reserved for file-derived free text surfaced
    by future report families (e.g. an unmapped-line description)."""
    return _NAME_PAIR.sub("•••", text)


@dataclass(frozen=True)
class RedactionStats:
    pans_masked: int = 0
    cells_dropped: int = 0
    header_cells_dropped: int = 0


def redact_words(words: list[Word]) -> tuple[list[Word], RedactionStats]:
    """Mask Luhn-valid 13-19 digit runs in the retained words, row by row.

    A card can be printed as several tokens (``4111 1111 1111 1111``), so the
    scan runs over each clustered row's text, not over single words; a run
    that would only form across two rows is not a card
    (tests/test_redaction.py::test_redact_words_scans_rows_not_the_whole_page).
    The scan is further scoped to consecutive digit-only cells within a row
    (see ``_DIGIT_CELL``): joining the whole row would let the PAN regex's
    greedy digit-and-space class run on into a neighboring non-PAN cell, such
    as an amount column's leading digits before its decimal point, which
    corrupts the Luhn check and masks nothing
    (tests/test_redaction.py::test_redact_words_masks_a_pan_split_across_four_words).
    Every covered word becomes ``••••``; the last keeps the final four
    digits. Output order and positions equal the input's.
    """
    masked: dict[int, str] = {}
    index_of = {id(w): i for i, w in enumerate(words)}
    pans = 0
    for row in cluster_rows(words):
        cells = sorted(row, key=lambda w: w.x0)
        i = 0
        while i < len(cells):
            if not _DIGIT_CELL.fullmatch(cells[i].text):
                i += 1
                continue
            j = i
            while j < len(cells) and _DIGIT_CELL.fullmatch(cells[j].text):
                j += 1
            group = cells[i:j]
            spans: list[tuple[int, int, int]] = []  # (start, end, word index)
            text_parts: list[str] = []
            pos = 0
            for w in group:
                if text_parts:
                    pos += 1
                spans.append((pos, pos + len(w.text), index_of[id(w)]))
                text_parts.append(w.text)
                pos += len(w.text)
            text = " ".join(text_parts)
            for m in _PAN_RUN.finditer(text):
                digits = re.sub(r"\D", "", m.group(0))
                if not (13 <= len(digits) <= 19 and _luhn_ok(digits)):
                    continue
                covered = [idx for s, e, idx in spans if s < m.end() and e > m.start()]
                if not covered:
                    continue
                pans += 1
                for idx in covered:
                    masked[idx] = "••••"
                masked[covered[-1]] = f"•••• {digits[-4:]}"
            i = j
    out = [replace(w, text=masked[i]) if i in masked else w for i, w in enumerate(words)]
    return out, RedactionStats(pans_masked=pans)


def redact(payload: PreviewPayload) -> PreviewPayload:
    """Defensive boundary pass (D8.4). The financial payload is aggregate-by-
    construction, so this is belt-to-suspenders: mask any PAN that leaked into a
    label. Name-masking is deliberately NOT run on curated mapping labels."""
    lines = [replace(line, line_item=mask_pans(line.line_item)) for line in payload.pnl_lines]
    return replace(payload, pnl_lines=lines)
