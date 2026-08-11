import pytest
import yaml
from defusedxml.common import EntitiesForbidden

from usali.mapping.draft_gen import _MAX_XML_BYTES, generate_draft


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


def test_generate_draft_rejects_xml_entities(tmp_path):
    catalog = tmp_path / "catalog.xml"
    catalog.write_text(
        '<!DOCTYPE catalog [<!ENTITY injected "sensitive">]>'
        "<catalog><G_TRX_CODE><TRX_CODE>&injected;</TRX_CODE></G_TRX_CODE></catalog>"
    )

    with pytest.raises(EntitiesForbidden):
        generate_draft(catalog, tmp_path / "draft.yaml")


def test_generate_draft_rejects_oversized_catalog(tmp_path):
    catalog = tmp_path / "catalog.xml"
    catalog.write_bytes(b" " * (_MAX_XML_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        generate_draft(catalog, tmp_path / "draft.yaml")
