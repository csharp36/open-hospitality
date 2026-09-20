from usali.adaptors.pdf import Word

# Known-but-unsupported PMS vendor header phrases -> vendor display name.
# Consulted ONLY when detect_report_signature() returns None, so the preview
# can say "looks like HotelKey" instead of a blank "unreadable". Sourced from
# docs/reference/pms-variants.md. A vendor here that later gains an adapter is
# harmless: detect() matches first, so recognition is never reached for it.
#
# Every phrase in a row must appear in the header window. Recognition only
# picks the display name for an "unsupported" message, so a multi-phrase row is
# about not naming the wrong vendor, not about parsing safety.
_VENDOR_SIGNATURES: list[tuple[tuple[str, ...], str]] = [
    (("HOTELKEY",), "HotelKey"),
    # HotelKey's report stamp. The real statistics sample (780 words; checked
    # 2026-09-20 with extract_words_from_bytes) never prints the vendor's name,
    # so the stamp is the header's only vendor evidence; pinned in
    # tests/test_recognition.py by test_hotelkey_is_named_from_its_report_run_stamp.
    # Either half alone names nobody: test_one_stamp_alone_names_nobody.
    (("REPORT RUN DATE:", "REPORT RUN TIME:"), "HotelKey"),
    (("SKYTOUCH",), "SkyTouch"),
    (("CHOICEADVANTAGE",), "SkyTouch"),
    (("CLOUDBEDS",), "Cloudbeds"),
    (("MEWS",), "Mews"),
    (("APALEO",), "Apaleo"),
    (("VISUAL MATRIX",), "Visual Matrix"),
    (("ROOMMASTER",), "roomMaster"),
    (("WEBREZPRO",), "WebRezPro"),
]
_HEADER_WORD_LIMIT = 120

_DISPLAY_NAMES = {"OPERA": "Opera", "AUTOCLERK": "AutoClerk", "SKYTOUCH": "SkyTouch",
                  "HOTELKEY": "HotelKey"}


def display_name(pms_source: str) -> str:
    """The vendor name the preview shows for a registered source; `str.title()`
    would print 'Skytouch' and 'Hotelkey'."""
    return _DISPLAY_NAMES.get(pms_source.upper(), pms_source.title())


def recognize_vendor(words: list[Word]) -> str | None:
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()
    return next(
        (
            name
            for phrases, name in _VENDOR_SIGNATURES
            if all(phrase in header_text for phrase in phrases)
        ),
        None,
    )
