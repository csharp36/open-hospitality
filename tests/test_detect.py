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


_ST_REGISTRY = [{"match": "REDSTONE TEST INN", "property_id": "STDEMO", "pms_source": "SKYTOUCH"}]


def _hdr(*tokens):
    # lay tokens out left-to-right on one header row; detect() only reads text of first 120 words
    return [Word(text=t, x0=10.0 + 12 * i, top=10.0) for i, t in enumerate(tokens)]


def test_detects_skytouch_hotel_journal():
    words = _hdr("Hotel", "Journal", "Summary", "Property", "Name:", "Redstone", "Test", "Inn")
    det = detect(words, _ST_REGISTRY)
    assert (det.pms_source, det.report_type) == ("SKYTOUCH", "hotel_journal")
    assert det.property_id == "STDEMO"


def test_detects_skytouch_hotel_statistics():
    words = _hdr("Hotel", "Statistics", "Property", "Name:", "Redstone", "Test", "Inn")
    det = detect(words, _ST_REGISTRY)
    assert (det.pms_source, det.report_type) == ("SKYTOUCH", "hotel_statistics")


def test_unknown_skytouch_section_raises():
    # a housekeeping section title is not a known signature -> detect() raises (the pack-skip path)
    words = _hdr("Vacant", "Room", "List", "Property", "Name:", "Redstone", "Test", "Inn")
    with pytest.raises(ValueError):
        detect(words, _ST_REGISTRY)
