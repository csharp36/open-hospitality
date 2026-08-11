from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.authkit import make_authkit
from usali.cli import app as cli_app
from usali.server import _MAX_PDF_BYTES, create_app

_VERIFIER, _MINT = make_authkit()

OPERA_PDF = Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf")


def _seed() -> None:
    runner = CliRunner()
    runner.invoke(cli_app, ["seed-properties", "mapping/properties.yaml"])
    runner.invoke(cli_app, ["seed-schedules", "mapping/usali_schedules.yaml"])
    runner.invoke(cli_app, ["seed-mappings", "mapping/opera.yaml"])


def test_ingest_endpoint_processes_upload(db_url, founding_org, tmp_path):
    _seed()
    client = TestClient(create_app(inbox_dir=tmp_path / "inbox",
                                   processed_dir=tmp_path / "processed",
                                   failed_dir=tmp_path / "failed",
                                   token_verifier=_VERIFIER))
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    with OPERA_PDF.open("rb") as f:
        resp = client.post("/ingest", files={"file": (OPERA_PDF.name, f, "application/pdf")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pms_source"] == "OPERA"
    assert body["property_id"] == "HISJ"
    assert body["staged"] == 14


def test_ingest_endpoint_reports_failure(db_url, founding_org, tmp_path):
    client = TestClient(create_app(inbox_dir=tmp_path / "inbox",
                                   processed_dir=tmp_path / "processed",
                                   failed_dir=tmp_path / "failed",
                                   token_verifier=_VERIFIER))
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    resp = client.post("/ingest", files={"file": ("junk.pdf", b"%PDF-1.4 garbage", "application/pdf")})
    assert resp.status_code == 422
    assert "junk.pdf" in resp.json()["detail"]


def test_ingest_rejects_path_components_without_touching_filesystem(
    db_url, founding_org, tmp_path
):
    client = TestClient(create_app(inbox_dir=tmp_path / "inbox",
                                   processed_dir=tmp_path / "processed",
                                   failed_dir=tmp_path / "failed",
                                   token_verifier=_VERIFIER))
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"

    for filename in ("../escape.pdf", r"..\escape.pdf", "/tmp/escape.pdf"):
        resp = client.post(
            "/ingest", files={"file": (filename, b"%PDF-1.4\n", "application/pdf")}
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "unsafe upload filename"

    assert not (tmp_path / "inbox").exists()
    assert not (tmp_path / "escape.pdf").exists()


def test_ingest_rejects_oversized_pdf_without_touching_filesystem(
    db_url, founding_org, tmp_path
):
    client = TestClient(create_app(inbox_dir=tmp_path / "inbox",
                                   processed_dir=tmp_path / "processed",
                                   failed_dir=tmp_path / "failed",
                                   token_verifier=_VERIFIER))
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    oversized = b"%PDF-1.4\n" + b"x" * _MAX_PDF_BYTES

    resp = client.post(
        "/ingest", files={"file": ("huge.pdf", oversized, "application/pdf")}
    )

    assert resp.status_code == 413
    assert resp.json()["detail"] == "PDF too large"
    assert not (tmp_path / "inbox").exists()


def test_ingest_rejects_non_pdf_without_touching_filesystem(db_url, founding_org, tmp_path):
    client = TestClient(create_app(inbox_dir=tmp_path / "inbox",
                                   processed_dir=tmp_path / "processed",
                                   failed_dir=tmp_path / "failed",
                                   token_verifier=_VERIFIER))
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"

    resp = client.post(
        "/ingest", files={"file": ("not-a-pdf.pdf", b"plain text", "application/pdf")}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "upload must be a PDF"
    assert not (tmp_path / "inbox").exists()
