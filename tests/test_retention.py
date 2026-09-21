"""The retention policy decides what a filed artifact keeps. Design D3-D6 in
docs/design/2026-09-21-ingestion-boundary-redaction-design.md."""

import json
from datetime import date
from pathlib import Path

from usali.adaptors.pdf import Word
from usali.adaptors.reader import read_words
from usali.adaptors.xlsx import XLSX_COL_STEP, XLSX_ROW_STEP
from usali.ingestion import _PIPELINES
from usali.retention import (
    RETENTION,
    RetainedSection,
    build_artifact,
    write_artifact,
    write_error_record,
)

SETTLEMENT = Path("tests/fixtures/hotelkey/Settlement By Payment Type.xlsx")


def test_every_pipeline_has_a_retention_policy():
    # Closed set: a report type cannot ship without saying what it retains.
    assert set(RETENTION) == set(_PIPELINES)


def _section(words: list[Word], source: str, report_type: str) -> RetainedSection:
    return RetainedSection(
        title="t", pms_source=source, report_type=report_type,
        property_id="HKDEMO", business_date=date(2026, 8, 13), words=words,
    )


def test_settlement_artifact_drops_identity_columns_and_the_user_cell():
    words = read_words(SETTLEMENT)
    art = build_artifact(
        source_file="s.xlsx", data=SETTLEMENT.read_bytes(), kind="single",
        sections=[_section(words, "HOTELKEY", "settlement")], sections_dropped=[],
    )
    texts = {w["text"] for s in art["sections"] for w in s["words"]}
    header = {w.text for w in words}
    for dropped in ("Guest Name", "First Name", "Last Name", "Account Name", "Username", "Remarks"):
        assert dropped in header  # the fixture really has the column
        assert dropped not in texts
    assert not any(t.startswith("User:") for t in texts)
    assert "Folio Number" in texts and "Amount" in texts
    assert art["redaction"]["cells_dropped"] > 0
    assert art["redaction"]["header_cells_dropped"] == 1


def test_settlement_artifact_keeps_no_value_from_a_dropped_column():
    # Every value under a dropped header is gone, not just the header cell.
    words = read_words(SETTLEMENT)
    art = build_artifact(
        source_file="s.xlsx", data=SETTLEMENT.read_bytes(), kind="single",
        sections=[_section(words, "HOTELKEY", "settlement")], sections_dropped=[],
    )
    kept = {(w["x0"], w["top"]) for w in art["sections"][0]["words"]}
    by_pos = {(w.x0, w.top): w.text for w in words}
    header_top = next(w.top for w in words if w.text == "Guest Name")
    guest_col = next(w.x0 for w in words if w.text == "Guest Name")
    below = [(x, t) for (x, t) in by_pos if x == guest_col and t > header_top]
    assert below, "fixture has no rows under Guest Name?"
    assert not any(pos in kept for pos in below)


def test_pdf_policy_keeps_all_words_after_pan_masking():
    words = [Word(text=t, x0=20.0 * i, top=10.0) for i, t in enumerate(
        ["Room", "Revenue", "4111", "1111", "1111", "1111"])]
    art = build_artifact(
        source_file="j.pdf", data=b"%PDF-", kind="single",
        sections=[_section(words, "SKYTOUCH", "hotel_journal")], sections_dropped=["A/R Aging"],
    )
    got = [w["text"] for w in art["sections"][0]["words"]]
    assert got == ["Room", "Revenue", "••••", "••••", "••••", "•••• 1111"]
    assert art["redaction"]["pans_masked"] == 1
    assert art["sections_dropped"] == ["A/R Aging"]
    assert art["sha256"] and art["bytes"] == 5 and art["kind"] == "single"


def test_write_artifact_names_by_stem_and_hash(tmp_path):
    art = build_artifact(source_file="pack.pdf", data=b"%PDF-x", kind="pack",
                         sections=[], sections_dropped=[])
    path = write_artifact(tmp_path / "done", art)
    assert path.name == f"pack.{art['sha256'][:8]}.redacted.json"
    assert json.loads(path.read_text())["source_file"] == "pack.pdf"


def test_write_error_record_holds_no_content(tmp_path):
    path = write_error_record(tmp_path / "fail", source_file="bad.pdf", data=b"%PDF-secret",
                              error="could not detect report type")
    rec = json.loads(path.read_text())
    assert set(rec) == {"source_file", "sha256", "bytes", "received_at", "error"}
    assert "secret" not in path.read_text()
    assert path.name.endswith(".error.json")


def _kept(art: dict, index: int = 0) -> list[Word]:
    return [Word(text=w["text"], x0=w["x0"], top=w["top"]) for w in art["sections"][index]["words"]]


def test_settlement_artifact_round_trips_through_the_adapter():
    # The words format exists so an artifact can be re-parsed (design D3).
    from usali.adaptors.hotelkey_settlement import parse_settlement

    words = read_words(SETTLEMENT)
    art = build_artifact(source_file="s.xlsx", data=SETTLEMENT.read_bytes(), kind="single",
                         sections=[_section(words, "HOTELKEY", "settlement")], sections_dropped=[])
    kept = _kept(art)
    original = parse_settlement(words, property_id="HKDEMO", business_date=date(2026, 8, 13))
    reparsed = parse_settlement(kept, property_id="HKDEMO", business_date=date(2026, 8, 13))
    assert [(r.pms_trx_code, r.pms_trx_desc, r.raw_amount) for r in reparsed] == \
           [(r.pms_trx_code, r.pms_trx_desc, r.raw_amount) for r in original]


def test_ar_aging_artifact_round_trips_and_keeps_only_label_and_aging_columns():
    from usali.adaptors.hotelkey_ar_aging import _AGING_COLUMNS, parse_ar_aging

    path = Path("tests/fixtures/hotelkey/AR Invoice Aging.xlsx")
    words = read_words(path)
    art = build_artifact(source_file="a.xlsx", data=path.read_bytes(), kind="single",
                         sections=[_section(words, "HOTELKEY", "ar_aging")], sections_dropped=[])
    kept = _kept(art)
    assert [(r.ledger_label, r.amount) for r in
            parse_ar_aging(kept, property_id="HKDEMO", business_date=date(2026, 8, 13))] == \
           [(r.ledger_label, r.amount) for r in
            parse_ar_aging(words, property_id="HKDEMO", business_date=date(2026, 8, 13))]
    # Header cells kept are the section label column plus the aging columns, nothing else.
    texts = {w.text for w in kept}
    assert set(_AGING_COLUMNS) <= texts


def test_a_kept_column_the_export_lacks_does_not_fail_retention():
    # The adapter accepts a settlement export without a Time column; retention
    # must not reject after the commit what the adapter accepted before it.
    words = [w for w in read_words(SETTLEMENT) if w.text != "Time"]
    art = build_artifact(source_file="s.xlsx", data=b"PK\x03\x04", kind="single",
                         sections=[_section(words, "HOTELKEY", "settlement")], sections_dropped=[])
    assert art["sections"][0]["words"]


def _grid(rows: dict[int, dict[str, str]]) -> list[Word]:
    """Sheet row number -> {column letter: text} on the synthetic XLSX grid."""
    return [
        Word(text=text, x0=(ord(col) - ord("A") + 1) * XLSX_COL_STEP, top=row * XLSX_ROW_STEP)
        for row, cells in rows.items()
        for col, text in cells.items()
    ]


def test_the_allowlist_is_applied_per_section_not_by_sheet_position():
    # A Summary block below the Details header keeps its own cells even when its
    # columns do not line up with the Details allowlist.
    words = _grid({
        1: {"A": "Lakeside Test Lodge"},
        2: {"A": "HKTEST"},
        3: {"H": "User: Sample TESTUSER"},
        4: {"A": "Settlement By Payment Type"},
        5: {"B": "Details"},
        6: {"B": "Account Category", "C": "Date", "D": "Guest Name",
            "E": "Payment Type", "F": "Amount"},
        7: {"A": "1", "B": "Reservation", "C": "2026-08-12", "D": "TESTGUEST ALPHA",
            "E": "MASTER", "F": "300"},
        8: {"A": "2", "B": "Reservation", "C": "2026-08-13", "D": "TESTGUEST BRAVO",
            "E": "VISA", "F": "200"},
        9: {"B": "Summary"},
        10: {"G": "Payment Type", "H": "Amount", "I": "Count"},
        11: {"A": "1", "G": "MASTER", "H": "300", "I": "1"},
        12: {"A": "END OF REPORT"},
    })
    art = build_artifact(source_file="s.xlsx", data=b"PK\x03\x04", kind="single",
                         sections=[_section(words, "HOTELKEY", "settlement")], sections_dropped=[])
    kept = _kept(art)
    texts = [w.text for w in kept]
    assert "Guest Name" not in texts and "TESTGUEST ALPHA" not in texts
    assert not any(t.startswith("User:") for t in texts)
    # The ordinals in column A survive: split_report classifies rows by them.
    ordinals = [w.text for w in kept if w.x0 == XLSX_COL_STEP]
    assert ordinals == ["Lakeside Test Lodge", "HKTEST", "Settlement By Payment Type",
                        "1", "2", "1", "END OF REPORT"]
    # The Summary block's own three columns survive, though none lines up with
    # the Details header's kept columns.
    summary = [w.text for w in kept if w.top == 11 * XLSX_ROW_STEP]
    assert summary == ["1", "MASTER", "300", "1"]
