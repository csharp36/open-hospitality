from usali.adaptors.pdf import Word

# Known-but-unsupported PMS vendor header phrases -> vendor display name.
# Consulted ONLY when detect_report_signature() returns None, so the preview
# can say "looks like HotelKey" instead of a blank "unreadable". Sourced from
# docs/reference/pms-variants.md. A vendor here that later gains an adapter is
# harmless: detect() matches first, so recognition is never reached for it.
_VENDOR_SIGNATURES: list[tuple[str, str]] = [
    ("HOTELKEY", "HotelKey"),
    ("SKYTOUCH", "SkyTouch"),
    ("CHOICEADVANTAGE", "SkyTouch"),
    ("CLOUDBEDS", "Cloudbeds"),
    ("MEWS", "Mews"),
    ("APALEO", "Apaleo"),
    ("VISUAL MATRIX", "Visual Matrix"),
    ("ROOMMASTER", "roomMaster"),
    ("WEBREZPRO", "WebRezPro"),
]
_HEADER_WORD_LIMIT = 120


def recognize_vendor(words: list[Word]) -> str | None:
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()
    return next((name for phrase, name in _VENDOR_SIGNATURES if phrase in header_text), None)
