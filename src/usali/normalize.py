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


def parse_paren_amount(text: str) -> Decimal:
    t = text.strip().replace(",", "")
    if t.startswith("(") and t.endswith(")"):
        return -Decimal(t[1:-1])
    return Decimal(t)


def parse_skytouch_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%m/%d/%Y").date()
