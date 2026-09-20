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


# --- The `HOTEL STATISTICS` phrase is not SkyTouch's alone --------------------
# Both windows below are the real samples' header words in extraction order
# (`~/Desktop/Sample Hotel/HotelKey/Hotel Statistics - HK.pdf` and the SkyTouch
# Standard Audit Pack of 2026-06-21; neither file is committed). HotelKey titles
# its statistics report identically and prints the property as a bare name,
# never behind a `Property Name:` banner; what it does print is the
# `Report Run Date:` / `Report Run Time:` stamp, which is its own anchor.

_HOTELKEY_STATISTICS_WINDOW = (
    "Summit", "Lodge", "Redstone,", "TX", "Date:", "Aug", "13,", "2026", "RDQSM",
    "Report", "Run", "Date:", "Aug", "14", "2026", "S",
    "Report", "Run", "Time:", "10:04:20", "AM", "MOCK", "DATA", "User:", "Sample",
    "DEVUSER", "RDQSM", "Hotel", "Statistics",
    "Room", "Statistics", "Description", "Actual", "Today", "M-T-D", "LY-M-T-D",
    "Y-T-D", "LY-T-D",
)

_SKYTOUCH_STATISTICS_WINDOW = (
    "Hotel", "Statistics", "Property", "Name:", "Econo", "Lodge",
    "Business", "Date:", "6/21/2026", "Property", "Code:", "NM070",
    "Shift:", "4", "User:*", "Room", "Statistics", "6/21/2026", "PTD",
    "Last", "Year", "PTD",
)


def test_hotelkey_statistics_resolves_to_hotelkey_not_skytouch():
    # Before OH-22 registered HotelKey this window resolved to nobody; the
    # SkyTouch-side pin is that it never resolves to SKYTOUCH.
    assert detect_report_signature(_words(*_HOTELKEY_STATISTICS_WINDOW)) == (
        "HOTELKEY",
        "hotel_statistics",
    )


def test_skytouch_statistics_still_matches_with_its_banner():
    words = _words(*_SKYTOUCH_STATISTICS_WINDOW)
    expected = ("SKYTOUCH", "hotel_statistics")
    assert detect_report_signature(words) == expected
    assert detect_report_signature(words, title="Hotel Statistics") == expected


def test_anchor_is_checked_on_the_title_path_too():
    # A pack section's title says "Hotel Statistics" but its page carries no
    # SkyTouch banner. The title path must not skip the anchor check.
    words = _words(
        "Hotel", "Statistics", "Summit", "Lodge", "Room", "Statistics", "Description"
    )
    assert detect_report_signature(words, title="Hotel Statistics") is None


def test_anchorless_rows_are_unchanged():
    # Only the HOTEL STATISTICS row carries an anchor; the others still match
    # on their phrase alone, with no banner anywhere in the window.
    assert detect_report_signature(_words("Hotel", "Journal", "Summary", "TOTALS")) == (
        "SKYTOUCH",
        "hotel_journal",
    )


# --- HotelKey registration (OH-22) --------------------------------------------

_HK_STAMP = ["HKTEST", "Report", "Run", "Date:", "Aug", "14", "2026", "Report", "Run", "Time:", "09:10:11", "AM"]


def test_hotelkey_statistics_resolves_to_hotelkey():
    words = _words("Lakeside", "Test", "Lodge,", "TX", "Date:", "Aug", "13,", "2026", *_HK_STAMP,
                   "Hotel", "Statistics", "Room", "Statistics")
    assert detect_report_signature(words) == ("HOTELKEY", "hotel_statistics")


def test_hotelkey_spreadsheets_resolve_by_title_cell():
    for title, report_type in [
        ("Settlement By Payment Type", "settlement"),
        ("All Payments", "all_payments"),
        ("AR Invoice Aging", "ar_aging"),
    ]:
        words = _words("Lakeside Test Lodge", "Date: Aug 13, 2026", "HKTEST",
                       "Report Run Date: Aug 14 2026", "Report Run Time: 09:10:11 AM",
                       "User: Sample TESTUSER", title)
        assert detect_report_signature(words) == ("HOTELKEY", report_type), title


def test_the_hotel_statistics_pair_is_decided_by_banner_not_table_order(monkeypatch):
    import usali.detect as d
    skytouch = _words("Hotel", "Statistics", "Property", "Name:", "Econo", "Lodge", "Business", "Date:")
    hotelkey = _words("Summit", "Lodge", "Date:", "Aug", "13,", "2026", *_HK_STAMP, "Hotel", "Statistics")
    monkeypatch.setattr(d, "_REPORT_SIGNATURES", list(reversed(d._REPORT_SIGNATURES)))
    assert detect_report_signature(skytouch) == ("SKYTOUCH", "hotel_statistics")
    assert detect_report_signature(hotelkey) == ("HOTELKEY", "hotel_statistics")


def test_a_generic_all_payments_title_without_the_stamp_is_nobody():
    assert detect_report_signature(_words("All", "Payments", "Report")) is None


def test_hotelkey_is_now_a_supported_source():
    from usali.detect import supported_pms_sources
    assert supported_pms_sources() == {"opera", "autoclerk", "skytouch", "hotelkey"}


def test_every_notice_source_is_a_supported_source():
    from usali.detect import SOURCE_NOTICES, supported_pms_sources
    assert {s.lower() for s in SOURCE_NOTICES} <= supported_pms_sources()
    assert "HOTELKEY" in SOURCE_NOTICES and SOURCE_NOTICES["HOTELKEY"].strip()
