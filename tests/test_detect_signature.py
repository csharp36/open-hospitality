# tests/test_detect_signature.py
import pytest

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


# --- Section titles decide identity (issue #78) -------------------------------


def test_section_title_decides_identity_not_a_body_column_heading():
    # A real SkyTouch "Cancellation List" carries RATE PLAN as a table COLUMN
    # HEADING, well inside the 120-word header window, so the bare "RATE PLAN"
    # signature matched it and routed the section to the AutoClerk rate_plan
    # adapter. A column heading is evidence about a report's COLUMNS, never
    # about its IDENTITY.
    words = _words(
        "Cancellation", "List", "GUEST", "NAME", "ARRIVAL", "NIGHTS",
        "RATE", "PLAN", "GTD", "SOURCE",
    )
    assert detect_report_signature(words, title="Cancellation List") is None


def test_section_title_still_matches_its_own_signature():
    # The title carries the signature, and a foreign phrase in the body no
    # longer competes with it.
    words = _words("Hotel", "Journal", "Summary", "RATE", "PLAN", "TOTALS")
    assert detect_report_signature(words, title="Hotel Journal Summary") == (
        "SKYTOUCH",
        "hotel_journal",
    )


def test_no_title_falls_back_to_the_header_window():
    # Standalone single-report files are not pack sections and have no title,
    # so they keep the header-window behaviour unchanged.
    assert detect_report_signature(_words("AUTOCLERK", "RATE", "PLAN")) == (
        "AUTOCLERK",
        "rate_plan",
    )


def test_blank_title_falls_back_to_the_header_window():
    # `pack._page_title` returns "" for a page with no words; that is an absent
    # title, not a title that matches nothing.
    assert detect_report_signature(_words("AUTOCLERK", "RATE", "PLAN"), title="   ") == (
        "AUTOCLERK",
        "rate_plan",
    )


def test_detect_threads_the_title_through_to_the_signature():
    # detect() must reach the same verdict as detect_report_signature(): the
    # section is unrecognised, which is what makes process_pack skip it.
    words = _words("Cancellation", "List", "RATE", "PLAN", "REDSTONE", "INN")
    registry = [{"match": "REDSTONE INN", "property_id": "RS1", "pms_source": "AUTOCLERK"}]
    with pytest.raises(ValueError, match="report type"):
        detect(words, registry, title="Cancellation List")
