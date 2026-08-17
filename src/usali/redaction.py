import re
from dataclasses import replace

from usali.preview import PreviewPayload

_PAN_RUN = re.compile(r"\b\d[\d -]{11,21}\d\b")
_NAME_PAIR = re.compile(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b")


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


def redact(payload: PreviewPayload) -> PreviewPayload:
    """Defensive boundary pass (D8.4). The financial payload is aggregate-by-
    construction, so this is belt-to-suspenders: mask any PAN that leaked into a
    label. Name-masking is deliberately NOT run on curated mapping labels."""
    lines = [replace(line, line_item=mask_pans(line.line_item)) for line in payload.pnl_lines]
    return replace(payload, pnl_lines=lines)
