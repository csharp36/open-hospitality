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

_PDF_HEADERS = {"content-type": "application/pdf"}


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
        content=SAMPLE_PDF.read_bytes(),
        headers=_PDF_HEADERS,
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
        "/api/preview", content=b"not a pdf", headers={"content-type": "text/plain"}
    )
    assert r.status_code == 415


def test_preview_bad_magic_is_415(client: TestClient) -> None:
    # Right content-type, but the bytes are not a PDF.
    r = client.post(
        "/api/preview", content=b"not a pdf at all", headers=_PDF_HEADERS
    )
    assert r.status_code == 415


def test_preview_too_large_is_413(client: TestClient) -> None:
    big = b"%PDF-" + b"0" * (10 * 1024 * 1024 + 1)
    r = client.post("/api/preview", content=big, headers=_PDF_HEADERS)
    assert r.status_code == 413


def test_preview_unsupported_vendor(client: TestClient, monkeypatch) -> None:
    import usali.server as srv
    from usali.adaptors.pdf import Word

    monkeypatch.setattr(
        srv,
        "extract_words_from_bytes",
        lambda data, max_pages=None: [
            Word(text=t, x0=float(i), top=0.0)
            for i, t in enumerate(["HotelKey", "Final", "Audit"])
        ],
    )
    r = client.post("/api/preview", content=b"%PDF-xx", headers=_PDF_HEADERS)
    assert r.json() == {
        "status": "unsupported",
        "vendor": "HotelKey",
        "reason": "vendor_not_supported",
    }


def test_preview_rate_limited_after_burst(client: TestClient) -> None:
    # 20/min per-IP window: the 21st preview from the same client is refused.
    for _ in range(20):
        client.post("/api/preview", content=b"not a pdf", headers=_PDF_HEADERS)
    r = client.post("/api/preview", content=b"not a pdf", headers=_PDF_HEADERS)
    assert r.status_code == 429
    # A 429 must tell the client when to retry.
    assert r.headers["Retry-After"] == "60"


def test_preview_response_omits_net_total(client: TestClient) -> None:
    # net_total is a never-a-balance-signal (D8): it stays server-side on the
    # PreviewPayload but must NOT be serialized to the client, so nothing
    # downstream can rebuild the dishonest signal from the response.
    r = client.post(
        "/api/preview",
        content=SAMPLE_PDF.read_bytes(),
        headers=_PDF_HEADERS,
    )
    assert r.status_code == 200
    assert "net_total" not in r.json()["payload"]


def test_preview_persists_nothing(tmp_path: Path) -> None:
    # The strongest persist-nothing proof: wrap the DB session factory in a spy
    # and assert the preview route never opened a session — plus the three
    # spooling dirs stay empty after a successful 200.
    calls: list[int] = []

    def spy_session_factory():  # type: ignore[no-untyped-def]
        calls.append(1)
        raise AssertionError("preview must never open a DB session")

    inbox = tmp_path / "in"
    processed = tmp_path / "p"
    failed = tmp_path / "f"
    verifier, _ = make_authkit()
    app = create_app(
        inbox_dir=inbox,
        processed_dir=processed,
        failed_dir=failed,
        session_factory=spy_session_factory,
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    client = TestClient(app)

    r = client.post(
        "/api/preview",
        content=SAMPLE_PDF.read_bytes(),
        headers=_PDF_HEADERS,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    # (a) the session factory was never called.
    assert calls == []
    # (b) nothing was spooled to any of the ingest dirs.
    for d in (inbox, processed, failed):
        assert not d.exists() or not any(d.iterdir())
