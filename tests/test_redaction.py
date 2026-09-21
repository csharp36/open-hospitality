# tests/test_redaction.py
from datetime import date

from usali.adaptors.pdf import Word
from usali.preview import PnlLine, PreviewPayload
from usali.redaction import mask_names, mask_pans, redact, redact_words


def test_mask_pans_masks_luhn_valid_card_only():
    assert mask_pans("paid 4111 1111 1111 1111 today") == "paid •••• 1111 today"
    assert mask_pans("ref 1234 5678 9012 3456") == "ref 1234 5678 9012 3456"


def test_mask_names_masks_capitalized_name_pairs():
    assert mask_names("guest John Smith checked out") == "guest ••• checked out"


def test_redact_scrubs_pans_but_preserves_mapping_labels():
    payload = PreviewPayload(
        pms_source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pnl_lines=[PnlLine("Settlements", "Credit Card", "Visa 4111111111111111", "0")],  # type: ignore[arg-type]
    )
    out = redact(payload)
    assert "4111111111111111" not in out.pnl_lines[0].line_item
    payload2 = PreviewPayload(
        pms_source="OPERA", report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pnl_lines=[PnlLine("Operated Departments", "Rooms", "Room Revenue", "0")],  # type: ignore[arg-type]
    )
    assert redact(payload2).pnl_lines[0].line_item == "Room Revenue"


def _row(*texts: str, top: float = 10.0) -> list[Word]:
    return [Word(text=t, x0=20.0 * i, top=top) for i, t in enumerate(texts)]


def test_redact_words_masks_a_pan_split_across_four_words():
    words = _row("Card", "4111", "1111", "1111", "1111", "12.50")
    out, stats = redact_words(words)
    assert [w.text for w in out] == ["Card", "••••", "••••", "••••", "•••• 1111", "12.50"]
    assert stats.pans_masked == 1


def test_redact_words_masks_a_single_token_pan_and_keeps_last4():
    out, stats = redact_words(_row("Visa", "4111111111111111"))
    assert [w.text for w in out] == ["Visa", "•••• 1111"]
    assert stats.pans_masked == 1


def test_redact_words_leaves_non_luhn_digit_runs_alone():
    # A 16-digit confirmation number that fails Luhn is not a card.
    out, stats = redact_words(_row("Conf", "1234567890123456"))
    assert [w.text for w in out] == ["Conf", "1234567890123456"]
    assert stats.pans_masked == 0


def test_redact_words_scans_rows_not_the_whole_page():
    # Two rows whose digits would form a Luhn-valid run only if joined
    # across the row boundary must not be masked.
    words = _row("A", "4111 1111", top=10.0) + _row("1111 1111", "B", top=40.0)
    out, stats = redact_words(words)
    assert [w.text for w in out] == ["A", "4111 1111", "1111 1111", "B"]
    assert stats.pans_masked == 0


def test_redact_words_preserves_positions_and_order():
    words = _row("Total", "Occupied", "Rooms", "62")
    out, _ = redact_words(words)
    assert [(w.x0, w.top) for w in out] == [(w.x0, w.top) for w in words]
    assert [w.text for w in out] == ["Total", "Occupied", "Rooms", "62"]
