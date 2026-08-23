import re
from datetime import date, datetime
from decimal import Decimal


def parse_amount(text: str) -> Decimal:
    cleaned = text.replace("$", "").replace(",", "").replace(" ", "")
    if cleaned in ("", "-"):
        return Decimal("0.00")
    return Decimal(cleaned)


def parse_opera_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%m-%d-%y").date()


def parse_autoclerk_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%m/%d/%Y").date()


# Accepts EITHER a paren-wrapped positive magnitude ``(nnn.nn)`` (an accounting
# negative) OR a bare signed amount ``-?nnn.nn``. Anything else — an unbalanced paren,
# a minus INSIDE the parens ``(-nnn.nn)``, a missing 2-dp fraction — fails the match and
# is rejected, so a malformed token can never be coerced into a wrong-signed number.
_PAREN_AMOUNT_RE = re.compile(r"^\((\d[\d,]*\.\d{2})\)$|^(-?[\d,]+\.\d{2})$")


def parse_paren_amount(text: str) -> Decimal:
    m = _PAREN_AMOUNT_RE.match(text.strip())
    if m is None:
        raise ValueError(f"unparseable paren amount: {text!r}")
    if m.group(1) is not None:  # (nnn.nn) -> a negative
        return -Decimal(m.group(1).replace(",", ""))
    return Decimal(m.group(2).replace(",", ""))


def parse_skytouch_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%m/%d/%Y").date()
