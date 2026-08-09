from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from tests.authkit import make_authkit
from usali.cli import app as cli_app
from usali.server import create_app

_VERIFIER, _MINT = make_authkit()

OPERA_PDF = Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf")


def _seed() -> None:
    runner = CliRunner()
    runner.invoke(cli_app, ["seed-schedules", "mapping/usali_schedules.yaml"])
    runner.invoke(cli_app, ["seed-mappings", "mapping/opera.yaml"])


def test_ingest_endpoint_processes_upload(db_url, tmp_path):
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


def test_ingest_endpoint_reports_failure(db_url, tmp_path):
    client = TestClient(create_app(inbox_dir=tmp_path / "inbox",
                                   processed_dir=tmp_path / "processed",
                                   failed_dir=tmp_path / "failed",
                                   token_verifier=_VERIFIER))
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    resp = client.post("/ingest", files={"file": ("junk.pdf", b"%PDF-1.4 garbage", "application/pdf")})
    assert resp.status_code == 422
    assert "junk.pdf" in resp.json()["detail"]
