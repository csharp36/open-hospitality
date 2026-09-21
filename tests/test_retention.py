"""The retention policy decides what a filed artifact keeps. Design D3-D6 in
docs/design/2026-09-21-ingestion-boundary-redaction-design.md."""

import json
from datetime import date
from pathlib import Path

from usali.adaptors.pdf import Word
from usali.adaptors.reader import read_words
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
