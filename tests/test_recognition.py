# tests/test_recognition.py
from usali.adaptors.pdf import Word
from usali.recognition import recognize_vendor


def _words(*texts: str) -> list[Word]:
    return [Word(text=t, x0=float(i), top=0.0) for i, t in enumerate(texts)]


def test_recognizes_known_unsupported_vendor():
    assert recognize_vendor(_words("HotelKey", "Final", "Audit", "Report")) == "HotelKey"


def test_returns_none_for_supported_or_unknown():
    assert recognize_vendor(_words("OPERA", "TRIAL", "BALANCE")) is None
    assert recognize_vendor(_words("random", "invoice")) is None
