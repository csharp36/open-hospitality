from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.authkit import make_authkit
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app

# A synthetic (fictitious-by-construction) Opera trial-balance PDF that already
# ships in the repo — the same sample the ingestion e2e suite seeds. NEVER a
# real PMS export.
SAMPLE_PDF = Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    # The /api/preview route is PUBLIC and stateless: it touches no DB, no auth,
    # no session. We still stand up the app with injected fakes so construction
    # never reaches settings-driven external clients, and a fresh app per test
    # gives each its own rate limiter.
    verifier, _ = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "in",
        processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    return TestClient(app)


def test_preview_ok_returns_pnl(client: TestClient) -> None:
    r = client.post(
        "/api/preview",
        files={"file": ("audit.pdf", SAMPLE_PDF.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["payload"]["pms_source"] == "OPERA"
    assert body["payload"]["report_type"] == "trial_balance"
    assert body["payload"]["pnl_lines"]
    assert "codes_needs_review" in body["payload"]


def test_preview_wrong_type_is_415(client: TestClient) -> None:
    r = client.post(
        "/api/preview", files={"file": ("x.txt", b"not a pdf", "text/plain")}
    )
    assert r.status_code == 415


def test_preview_bad_magic_is_415(client: TestClient) -> None:
    # Right content-type, but the bytes are not a PDF.
    r = client.post(
        "/api/preview",
        files={"file": ("x.pdf", b"not a pdf at all", "application/pdf")},
    )
    assert r.status_code == 415


def test_preview_too_large_is_413(client: TestClient) -> None:
    big = b"%PDF-" + b"0" * (10 * 1024 * 1024 + 1)
    r = client.post(
        "/api/preview", files={"file": ("big.pdf", big, "application/pdf")}
    )
    assert r.status_code == 413


def test_preview_unsupported_vendor(client: TestClient, monkeypatch) -> None:
    import usali.server as srv
    from usali.adaptors.pdf import Word

    monkeypatch.setattr(
        srv,
        "extract_words_from_bytes",
        lambda data: [
            Word(text=t, x0=float(i), top=0.0)
            for i, t in enumerate(["HotelKey", "Final", "Audit"])
        ],
    )
    r = client.post(
        "/api/preview", files={"file": ("hk.pdf", b"%PDF-xx", "application/pdf")}
    )
    assert r.json() == {
        "status": "unsupported",
        "vendor": "HotelKey",
        "reason": "vendor_not_supported",
    }


def test_preview_rate_limited_after_burst(client: TestClient) -> None:
    # 20/min window: the 21st preview from the same client is refused.
    files = {"file": ("x.pdf", b"not a pdf", "application/pdf")}
    for _ in range(20):
        client.post("/api/preview", files=files)
    r = client.post("/api/preview", files=files)
    assert r.status_code == 429
