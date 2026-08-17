# tests/test_detect_signature.py
from usali.adaptors.pdf import Word
from usali.detect import detect, detect_report_signature


def _words(*texts: str) -> list[Word]:
    return [Word(text=t, x0=float(i), top=0.0) for i, t in enumerate(texts)]


def test_detect_report_signature_matches_supported_header():
    assert detect_report_signature(_words("OPERA", "TRIAL", "BALANCE")) == (
        "OPERA",
        "trial_balance",
    )


def test_detect_report_signature_none_when_unknown():
    assert detect_report_signature(_words("HotelKey", "Final", "Audit")) is None


def test_detect_still_resolves_property_via_registry():
    words = _words("OPERA", "TRIAL", "BALANCE", "REDSTONE", "INN")
    registry = [{"match": "REDSTONE INN", "property_id": "RS1", "pms_source": "OPERA"}]
    det = detect(words, registry)
    assert (det.pms_source, det.report_type, det.property_id) == (
        "OPERA",
        "trial_balance",
        "RS1",
    )
