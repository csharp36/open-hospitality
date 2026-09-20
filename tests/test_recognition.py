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


def test_hotelkey_is_named_from_its_report_run_stamp():
    # The real HotelKey statistics sample never prints the word "HotelKey";
    # its top-right stamp is the only vendor evidence in the header.
    words = _words(
        "Summit", "Lodge", "Date:", "Aug", "13,", "2026",
        "Report", "Run", "Date:", "Aug", "14", "2026",
        "Report", "Run", "Time:", "10:04:20", "AM", "User:", "Sample",
        "Hotel", "Statistics",
    )
    assert recognize_vendor(words) == "HotelKey"


def test_one_stamp_alone_names_nobody():
    # Both halves of the stamp are required: "Report Run Date:" on its own is
    # not enough to put a vendor's name in front of a customer.
    words = _words("Summit", "Lodge", "Report", "Run", "Date:", "Aug", "14", "2026")
    assert recognize_vendor(words) is None
    words = _words("Summit", "Lodge", "Report", "Run", "Time:", "10:04:20", "AM")
    assert recognize_vendor(words) is None


def test_every_supported_source_has_a_display_name():
    # A source registered in detect without a spelling here would print through
    # str.title() ("Skytouch", "Hotelkey") in the preview.
    from usali.detect import supported_pms_sources
    from usali.recognition import _DISPLAY_NAMES

    assert set(_DISPLAY_NAMES) == {s.upper() for s in supported_pms_sources()}
