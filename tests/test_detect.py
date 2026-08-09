import json
from pathlib import Path

import pytest

from usali.adaptors.pdf import Word
from usali.detect import Detection, detect

_REGISTRY: list[dict[str, str]] = [
    {"match": "HOLIDAY INN & SUITES SAN JOSE", "property_id": "HISJ", "pms_source": "OPERA"},
    {"match": "SURESTAY PLUS BY BW", "property_id": "SSSJ", "pms_source": "AUTOCLERK"},
]


def _words(fixture: str) -> list[Word]:
    data = json.loads(Path(fixture).read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def test_detects_opera_trial_balance():
    det = detect(_words("tests/fixtures/opera_trial_balance_words.json"), _REGISTRY)
    assert det == Detection(pms_source="OPERA", report_type="trial_balance", property_id="HISJ")


def test_detects_autoclerk_transaction_summary():
    det = detect(
        _words("tests/fixtures/autoclerk_transaction_summary_words.json"),
        _REGISTRY,
    )
    assert det == Detection(
        pms_source="AUTOCLERK", report_type="transaction_summary", property_id="SSSJ"
    )


def test_unknown_report_raises():
    with pytest.raises(ValueError, match="report type"):
        detect([Word(text="Mystery", x0=0.0, top=0.0)], _REGISTRY)


def test_unknown_property_raises():
    words = [Word(text="Trial", x0=0.0, top=0.0), Word(text="Balance", x0=20.0, top=0.0)]
    with pytest.raises(ValueError, match="property"):
        detect(words, _REGISTRY)


def test_detects_opera_manager_flash():
    det = detect(_words("tests/fixtures/opera_manager_flash_words.json"), _REGISTRY)
    assert det == Detection(pms_source="OPERA", report_type="manager_flash", property_id="HISJ")


def test_detects_autoclerk_manager_report():
    det = detect(
        _words("tests/fixtures/autoclerk_manager_report_words.json"), _REGISTRY
    )
    assert det == Detection(
        pms_source="AUTOCLERK", report_type="manager_report", property_id="SSSJ"
    )


def test_detects_opera_market_stats():
    det = detect(_words("tests/fixtures/opera_market_stats_words.json"), _REGISTRY)
    assert det == Detection(pms_source="OPERA", report_type="market_stats", property_id="HISJ")


def test_detects_autoclerk_rate_plan():
    det = detect(
        _words("tests/fixtures/autoclerk_rate_plan_words.json"), _REGISTRY
    )
    assert det == Detection(pms_source="AUTOCLERK", report_type="rate_plan", property_id="SSSJ")
