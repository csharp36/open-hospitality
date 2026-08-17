from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from defusedxml.common import DefusedXmlException

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


def test_generate_draft_rejects_dtd_and_entities(tmp_path):
    # An entity injection is declared inside a DOCTYPE; forbid_dtd rejects the
    # DOCTYPE outright (DTDForbidden), and forbid_entities would reject the
    # expansion -- both subclass DefusedXmlException. Asserting the base keeps
    # the test robust to which guard fires first while pinning the security
    # property: no DTD/entity construct is ever parsed.
    catalog = tmp_path / "catalog.xml"
    catalog.write_text(
        '<!DOCTYPE catalog [<!ENTITY injected "sensitive">]>'
        "<catalog><G_TRX_CODE><TRX_CODE>&injected;</TRX_CODE></G_TRX_CODE></catalog>"
    )

    with pytest.raises(DefusedXmlException):
        generate_draft(catalog, tmp_path / "draft.yaml")


def test_generate_draft_rejects_oversized_catalog(tmp_path):
    catalog = tmp_path / "catalog.xml"
    catalog.write_bytes(b" " * (_MAX_XML_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        generate_draft(catalog, tmp_path / "draft.yaml")


def test_generate_draft_size_guard_is_fail_closed_before_reading(tmp_path, monkeypatch):
    # The size bound must fire on stat, BEFORE read_bytes -- otherwise a
    # multi-GB export OOMs the process before the guard runs. Report an
    # oversized stat and make read_bytes explode: a passing test proves the
    # read never happens. This kills the read-before-check mutation, which the
    # cheap (_MAX+1)-byte test above cannot distinguish.
    catalog = tmp_path / "catalog.xml"
    catalog.write_bytes(b"<catalog/>")

    monkeypatch.setattr(
        Path, "stat", lambda self, *a, **k: SimpleNamespace(st_size=_MAX_XML_BYTES + 1)
    )

    def _boom(self, *a, **k):
        raise AssertionError("read_bytes must not run on an oversized catalog")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    with pytest.raises(ValueError, match="exceeds"):
        generate_draft(catalog, tmp_path / "draft.yaml")
