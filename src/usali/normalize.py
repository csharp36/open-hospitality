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
