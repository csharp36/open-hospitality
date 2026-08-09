import yaml

from usali.mapping.draft_gen import generate_draft


def test_generate_draft_from_catalog(tmp_path):
    out = tmp_path / "draft.yaml"
    n = generate_draft(
        "docs/reference/sources/opera/Opera - Transaction Codes By Transaction Code.xml",
        out,
        edition=12,
    )
    rows = yaml.safe_load(out.read_text())
    assert n == len(rows) >= 400
    sample = rows[0]
    assert set(sample) >= {"source", "code", "edition", "schedule_id", "major", "sub",
                           "line_item", "confidence", "review_status"}
    assert all(r["confidence"] == "LOW" for r in rows)
    assert all(r["source"] == "OPERA" for r in rows)
