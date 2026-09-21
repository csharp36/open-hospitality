import re
from dataclasses import dataclass, replace

from usali.adaptors.pdf import Word, cluster_rows
from usali.preview import PreviewPayload

_PAN_RUN = re.compile(r"\b\d[\d -]{11,21}\d\b")
_NAME_PAIR = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")
_DIGIT_CELL = re.compile(r"\d+")
# Visa, Mastercard, Amex, Discover/JCB/UnionPay — the issuer identifiers a card
# printed on a hotel report can open with. A figure column opening with 1, 2, 7,
# 8, 9 or 0 is therefore never a candidate.
_ISSUER_DIGITS = "3456"
# A card broken across cells prints as four groups of four, or as Amex's 4-6-5.
_CARD_CELL_SHAPES: tuple[tuple[int, ...], ...] = ((4, 4, 4, 4), (4, 6, 5))


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


def _card_span(lengths: list[int], k: int) -> int | None:
    """How many consecutive digit cells from ``k`` a card SHAPE covers, or None."""
    if 13 <= lengths[k] <= 19:
        return 1
    for shape in _CARD_CELL_SHAPES:
        if tuple(lengths[k : k + len(shape)]) == shape:
            return len(shape)
    return None


def redact_words(words: list[Word]) -> tuple[list[Word], RedactionStats]:
    """Mask card-shaped runs of digit cells in the retained words, row by row.

    A digit cell is a word that is digits and nothing else, so ``2026-08-01``
    and ``12.50`` are not digit cells. Within a run of consecutive digit cells
    the scan runs left to right and masks exactly three shapes: one cell of
    13-19 digits, four consecutive 4-digit cells, and the 4-6-5 of an Amex
    (``_CARD_CELL_SHAPES``). Each also has to pass the Luhn check and open with
    an issuer digit (``_ISSUER_DIGITS``). Every other run of figures is left as
    it is, which is what
    tests/test_redaction.py::test_redact_words_masks_nothing_in_a_report_with_no_card_numbers
    holds for every committed sample: a statistics row, a folio number and an
    ISO date beside amounts all come through unchanged. A card printed beside
    another digit cell is still found, because the shapes are matched at each
    starting cell rather than over the whole run
    (test_redact_words_masks_a_pan_followed_by_a_digit_cell). Rows come from
    ``cluster_rows``, so digits that would only form a shape across two rows
    never meet (test_redact_words_scans_rows_not_the_whole_page).

    Every cell a match covers becomes ``••••``; the last keeps the final four
    digits, and each match counts one in ``pans_masked``. Output order and
    positions equal the input's.
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
            lengths = [len(w.text) for w in group]

            k = 0
            while k < len(group):
                span = _card_span(lengths, k)
                digits = "".join(w.text for w in group[k : k + span]) if span else ""
                if span is None or digits[0] not in _ISSUER_DIGITS or not _luhn_ok(digits):
                    k += 1
                    continue
                pans += 1
                for w in group[k : k + span]:
                    masked[index_of[id(w)]] = "••••"
                masked[index_of[id(group[k + span - 1])]] = f"•••• {digits[-4:]}"
                k += span
            i = j
    out = [replace(w, text=masked[i]) if i in masked else w for i, w in enumerate(words)]
    return out, RedactionStats(pans_masked=pans)


def redact(payload: PreviewPayload) -> PreviewPayload:
    """Defensive boundary pass (D8.4). The financial payload is aggregate-by-
    construction, so this is belt-to-suspenders: mask any PAN that leaked into a
    label. Name-masking is deliberately NOT run on curated mapping labels."""
    lines = [replace(line, line_item=mask_pans(line.line_item)) for line in payload.pnl_lines]
    return replace(payload, pnl_lines=lines)
