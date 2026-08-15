from datetime import date
from decimal import Decimal

import pytest

from usali.normalize import parse_amount, parse_opera_date


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10,395.00", Decimal("10395.00")),
        ("$ 6,129.03", Decimal("6129.03")),
        ("-$ 428.35", Decimal("-428.35")),
        ("- 3,496.32", Decimal("-3496.32")),
        ("-149.75", Decimal("-149.75")),
        ("0.00", Decimal("0.00")),
    ],
)
def test_parse_amount(text, expected):
    assert parse_amount(text) == expected


def test_parse_opera_date():
    assert parse_opera_date("07-07-26") == date(2026, 7, 7)


def test_parse_autoclerk_date():
    from usali.normalize import parse_autoclerk_date

    assert parse_autoclerk_date("07/07/2026") == date(2026, 7, 7)


def test_parse_paren_amount():
    from usali.normalize import parse_paren_amount

    assert parse_paren_amount("(1234.56)") == Decimal("-1234.56")
    assert parse_paren_amount("7,890.12") == Decimal("7890.12")
    assert parse_paren_amount("0.00") == Decimal("0.00")


def test_parse_skytouch_date():
    from usali.normalize import parse_skytouch_date

    assert parse_skytouch_date("6/21/2026") == date(2026, 6, 21)
