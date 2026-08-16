# tests/test_redaction.py
from datetime import date

from usali.preview import PnlLine, PreviewPayload
from usali.redaction import mask_names, mask_pans, redact


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
