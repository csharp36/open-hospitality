import json
from datetime import date
from pathlib import Path

from usali.adaptors.opera_trial_balance import extract_business_date
from usali.adaptors.pdf import Word


def _load_words() -> list[Word]:
    data = json.loads(Path("tests/fixtures/opera_trial_balance_words.json").read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def test_extract_opera_business_date():
    assert extract_business_date(_load_words()) == date(2026, 7, 7)
