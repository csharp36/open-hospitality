import re
from datetime import date
from decimal import Decimal

from usali.adaptors.pdf import Word, cluster_rows
from usali.normalize import parse_amount, parse_autoclerk_date
from usali.schemas import StagedRecord

_NUMBER_RE = re.compile(r"\d[\d,]*\.\d{2}$")  # a value's numeric part, ends in cents
_CURRENCY_RE = re.compile(r"^-?\$$")  # a "$" or "-$" token that starts a value
_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_CATEGORY_MAX_X0 = 120.0  # category labels sit in the far-left column


def _slug(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")


def _amount_start(token: str) -> bool:
    return bool(_CURRENCY_RE.match(token) or _NUMBER_RE.search(token))


def _split_name_and_today(tokens: list[str]) -> tuple[str, Decimal] | None:
    # Returns (name, TODAY amount) for a type/total row, or None if the row carries no amount.
    idx = next((i for i, t in enumerate(tokens) if _amount_start(t)), None)
    if idx is None:
        return None
    name = " ".join(tokens[:idx]).strip()
    value_tokens: list[str] = []
    for t in tokens[idx:]:
        value_tokens.append(t)
        if _NUMBER_RE.search(t):  # first complete value (TODAY) ends at the first number token
            break
    return name, parse_amount("".join(value_tokens))


def extract_business_date(words: list[Word]) -> date:
    for w in words:
        if _DATE_RE.match(w.text):
            return parse_autoclerk_date(w.text)
    raise ValueError("no Autoclerk business date (MM/DD/YYYY) found in report")


def parse_transaction_summary(
    words: list[Word], *, property_id: str, business_date: date, y_tol: float = 3.0
) -> list[StagedRecord]:
    records: list[StagedRecord] = []
    category: str | None = None
    grand_total_today: Decimal | None = None
    saw_grand_total = False
    started = False

    for row in cluster_rows(words, y_tol):
        cells = sorted(row, key=lambda w: w.x0)
        tokens = [w.text for w in cells]
        if "Category" in tokens and "TODAY" in tokens:
            # The column-header row repeats on every page (report can span pages);
            # skip it every time, not just before the first data row.
            started = True
            continue
        if not started:
            continue
        first_upper = tokens[0].upper() if tokens else ""
        if first_upper.startswith("TOTAL"):
            continue
        if first_upper == "GRAND":
            saw_grand_total = True
            parsed = _split_name_and_today(tokens)
            if parsed is not None:
                grand_total_today = parsed[1]
            continue
        parsed = _split_name_and_today(tokens)
        if parsed is None:
            # No amount -> a category header (only when it sits in the far-left column).
            if cells and cells[0].x0 < _CATEGORY_MAX_X0:
                category = " ".join(tokens).strip()
            continue
        if category is None:
            continue
        name, today = parsed
        records.append(
            StagedRecord(
                property_id=property_id,
                pms_source="AUTOCLERK",
                report_type="transaction_summary",
                business_date=business_date,
                pms_trx_code=f"{_slug(category)}|{_slug(name)}",
                pms_trx_desc=f"{category} - {name}",
                raw_amount=today,
                section=category,
            )
        )

    # Reconciliation must be LOUD, never a silent no-op: a missing or unparsable GRAND
    # TOTAL means we could not verify the parse, so refuse rather than return unverified data.
    if not saw_grand_total:
        raise ValueError("no GRAND TOTAL row found in report; cannot verify the parse")
    if grand_total_today is None:
        raise ValueError("GRAND TOTAL row found but its TODAY amount could not be parsed")
    total = sum((r.raw_amount for r in records), Decimal("0"))
    if total != grand_total_today:
        raise ValueError(f"parsed TODAY sum {total} != report GRAND TOTAL {grand_total_today}")
    return records
