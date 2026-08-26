"""Portal read API (P7 Task 2) over the seeded six-PDF database.

Endpoint tests via FastAPI TestClient with a session factory bound to the test
engine (no env-var coupling): properties picker, SOS in both date modes with the
known totals, exactly-one-of validation and 404/422 error mapping, drill-through
reconciliation, coverage shape — and the P6 invariant that no value anywhere in
the payloads is a float.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.server import create_app

_VERIFIER, _MINT = make_authkit()


def _make_client(db_engine: Engine, tmp_path: Path) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=_VERIFIER,
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    return client


@pytest.fixture
def client(
    db_engine: Engine, db_session: Session, seed_six_pdfs: None, tmp_path: Path
) -> TestClient:
    # L4: the minted accountant's authority is its org-wide DB grant.
    grant_role(db_session, "accountant")
    return _make_client(db_engine, tmp_path)


@pytest.fixture
def bare_client(
    db_engine: Engine, db_session: Session, founding_org: object, tmp_path: Path
) -> TestClient:
    """Client over the truncated test database — no report data seeded.
    The founding org row DOES exist (as in every real database): since L3
    a request must resolve its active org before it can read anything."""
    grant_role(db_session, "accountant")
    return _make_client(db_engine, tmp_path)


def _assert_no_floats(value: object, path: str = "$") -> None:
    assert not isinstance(value, float), f"float leaked into payload at {path}: {value!r}"
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _assert_no_floats(item, f"{path}[{i}]")


def test_properties_endpoint(client):
    r = client.get("/api/properties")
    assert r.status_code == 200, r.text
    props = r.json()
    assert {(p["property_id"], p["pms_source"]) for p in props} == {
        ("HISJ", "OPERA"),
        ("SSSJ", "AUTOCLERK"),
    }
    hisj = next(p for p in props if p["property_id"] == "HISJ")
    assert hisj["first_date"] == hisj["last_date"] == "2026-07-07"


def test_properties_endpoint_empty_database(bare_client):
    r = bare_client.get("/api/properties")
    assert r.status_code == 200 and r.json() == []


def test_sos_endpoint_single_date(client):
    r = client.get("/api/sos", params={"property": "HISJ", "date": "2026-07-07"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["total_operating_revenue"]) == Decimal("10866.37")
    assert body["business_date"] == "2026-07-07"
    assert body["date_from"] is None and body["date_to"] is None
    assert body["statistics"]  # pivot present
    # Per-line misc income is part of the contract (drill-through targets).
    # Facts are Numeric(15,4), so exact strings carry the DB scale ("410.0000");
    # compare as Decimals, per the P6 convention.
    assert [(m["major"], m["sub_category"], m["line_item"]) for m in body["misc_income"]] == [
        ("Miscellaneous Income", "Parking", "Parking")
    ]
    assert Decimal(body["misc_income"][0]["total"]) == Decimal("410.00")
    assert Decimal(body["misc_income_total"]) == Decimal("410.00")
    rooms = body["operated_departments"][0]
    assert rooms["sub_category"] == "Rooms" and Decimal(rooms["total"]) == Decimal("10395.00")


def test_sos_endpoint_range(client):
    r = client.get(
        "/api/sos",
        params={"property": "HISJ", "from": "2026-07-01", "to": "2026-07-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert Decimal(body["total_operating_revenue"]) == Decimal("10866.37")
    assert body["business_date"] is None
    assert body["date_from"] == "2026-07-01" and body["date_to"] == "2026-07-31"


def test_sos_payload_has_no_floats(client):
    r = client.get("/api/sos", params={"property": "HISJ", "date": "2026-07-07"})
    assert r.status_code == 200, r.text
    _assert_no_floats(r.json())


def test_sos_validation(client):
    # Neither mode given.
    assert client.get("/api/sos", params={"property": "HISJ"}).status_code == 422
    # Both modes given.
    assert (
        client.get(
            "/api/sos",
            params={
                "property": "HISJ",
                "date": "2026-07-07",
                "from": "2026-07-01",
                "to": "2026-07-31",
            },
        ).status_code
        == 422
    )
    # Half a range.
    assert (
        client.get("/api/sos", params={"property": "HISJ", "from": "2026-07-01"}).status_code
        == 422
    )
    # Inverted range.
    r = client.get(
        "/api/sos",
        params={"property": "HISJ", "from": "2026-07-31", "to": "2026-07-01"},
    )
    assert r.status_code == 422 and "after date_to" in r.json()["detail"]
    # Malformed date -> FastAPI validation.
    assert (
        client.get("/api/sos", params={"property": "HISJ", "date": "not-a-date"}).status_code
        == 422
    )
    # Property with no facts -> 404.
    r = client.get("/api/sos", params={"property": "NOPE", "date": "2026-07-07"})
    assert r.status_code == 404 and "no financial facts" in r.json()["detail"]


def test_sos_endpoint_returns_labor_sections(client, db_session):
    # B3 lockstep: an approved+promoted timecard surfaces on /api/sos as
    # Schedule 14 (est cost) + Schedule 15 (hours). Seed labor on the fixture's
    # revenue date so the SOS anchors on real financial facts.
    from tests.test_reporting_labor import _seed_labor

    _seed_labor(db_session, date(2026, 7, 7))  # TWO employees -> cost not suppressed
    r = client.get("/api/sos", params={"property": "HISJ", "date": "2026-07-07"})
    assert r.status_code == 200, r.text
    body = r.json()
    # Schedule 14: est cost is the SUM of both employees (2 × 8h × $20 = $320) and
    # SHOWS as a string (not null) — a two-employee department leaks no rate.
    assert body["payroll_expense_total"] == "320.0000"
    hk = next(line for line in body["payroll_expense"] if line["department"] == "Housekeeping")
    assert hk["est_cost"] == "320.0000"
    # Schedule 15: hours (2 × 8h).
    assert body["labor_hours_total"] == "16.00"
    assert body["labor_ot_hours_total"] == "0.00"
    assert body["labor_suppressed_departments"] == 0
    assert body["labor_unpriced_hours"] == "0"
    _assert_no_floats(body)


def test_sos_endpoint_suppresses_single_employee_cost(client, db_session):
    # A single-employee department serializes est_cost as null (JSON None) and is
    # excluded from the total, with a suppressed-department count for the UI note.
    from tests.test_reporting_labor import _seed_labor

    _seed_labor(db_session, date(2026, 7, 7), employees=1)
    r = client.get("/api/sos", params={"property": "HISJ", "date": "2026-07-07"})
    assert r.status_code == 200, r.text
    body = r.json()
    hk = next(line for line in body["payroll_expense"] if line["department"] == "Housekeeping")
    assert hk["est_cost"] is None
    assert hk["hours"] == "8.00"  # hours still carry
    assert body["payroll_expense_total"] == "0"
    assert body["labor_suppressed_departments"] == 1
    _assert_no_floats(body)


def test_sos_endpoint_returns_the_variance_block(client, db_session):
    # C3 lockstep: a processed pay run with estimate + actual facts surfaces on
    # /api/sos as ONE nested labor_variance object. Seeded via Task 1's world
    # (Housekeeping: est 640.00 / actual 704.00 -> variance 64.00 = 10% ->
    # alert; Front Desk: solo -> suppressed) layered onto the fixture's revenue
    # facts (the SOS needs revenue to anchor on). Range mode is `from`/`to`.
    from tests.test_reporting_variance import _seed_variance_world

    _seed_variance_world(db_session)
    r = client.get(
        "/api/sos",
        params={"property": "HISJ", "from": "2026-07-01", "to": "2026-07-31"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    v = body["labor_variance"]
    assert v is not None
    assert v["est_total"] == "640.00"
    assert v["actual_total"] == "704.00"
    assert v["variance_total"] == "64.00"
    assert v["burden_total"] == "70.40"
    assert v["alert"] is True
    assert v["periods"] == ["2026-07-06..2026-07-19"]
    assert v["suppressed_departments"] == 1
    line = next(ln for ln in v["lines"] if ln["department"] == "Housekeeping")
    assert line["est_cost"] == "640.00"
    assert line["actual_gross"] == "704.00"
    assert line["variance"] == "64.00" and line["alert"] is True
    # Suppressed line serializes with JSON nulls — hours still carry.
    solo = next(ln for ln in v["lines"] if ln["department"] == "Front Desk")
    assert solo["est_cost"] is None and solo["actual_gross"] is None
    assert solo["employer_burden"] is None and solo["variance"] is None
    assert Decimal(solo["hours_actual"]) == Decimal("8.00")
    assert solo["alert"] is False
    _assert_no_floats(body)


def test_sos_endpoint_variance_is_null_without_runs(client):
    # Revenue facts exist (the fixture) but no processed pay run touches the
    # window: the block serializes as JSON null, not an empty shell.
    r = client.get("/api/sos", params={"property": "HISJ", "date": "2026-07-07"})
    assert r.status_code == 200, r.text
    assert r.json()["labor_variance"] is None


def test_sos_endpoint_labor_sections_empty_without_labor(client):
    r = client.get("/api/sos", params={"property": "HISJ", "date": "2026-07-07"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payroll_expense"] == []
    assert body["payroll_expense_total"] == "0"
    assert body["labor_hours_total"] == "0"
    assert body["labor_fte"] is None


def test_line_transactions_endpoint(client):
    r = client.get(
        "/api/sos/line/transactions",
        params={
            "property": "HISJ",
            "major": "Miscellaneous Income",
            "sub": "Parking",
            "line_item": "Parking",
            "from": "2026-07-07",
            "to": "2026-07-07",
        },
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert {t["pms_trx_code"] for t in rows} == {"5105"}
    assert sum(Decimal(t["amount"]) for t in rows) == Decimal("410.00")
    t = rows[0]
    assert t["pms_source"] == "OPERA" and t["business_date"] == "2026-07-07"
    assert t["pms_trx_desc"]
    assert t["source_file"].endswith(".pdf")
    _assert_no_floats(rows)


def test_line_transactions_endpoint_validation(client):
    base = {
        "property": "HISJ",
        "major": "Miscellaneous Income",
        "sub": "Parking",
        "line_item": "Parking",
    }
    # Unknown line is empty, not an error.
    r = client.get(
        "/api/sos/line/transactions",
        params={**base, "major": "Nope", "from": "2026-07-07", "to": "2026-07-07"},
    )
    assert r.status_code == 200 and r.json() == []
    # Inverted range -> 422 with the reporting message.
    r = client.get(
        "/api/sos/line/transactions",
        params={**base, "from": "2026-07-31", "to": "2026-07-01"},
    )
    assert r.status_code == 422 and "after date_to" in r.json()["detail"]
    # Missing required params -> FastAPI validation.
    assert client.get("/api/sos/line/transactions", params=base).status_code == 422


def test_coverage_endpoint(client):
    r = client.get("/api/coverage", params={"edition": 12})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {s["pms_source"] for s in body["sources"]} == {"AUTOCLERK", "OPERA"}
    opera = next(s for s in body["sources"] if s["pms_source"] == "OPERA")
    # The Parking mapping is the known needs-review worklist row.
    assert any(
        e["code"] == "5105" and e["line_item"] == "Parking"
        for e in opera["financial"]["needs_review"]
    )
    assert isinstance(opera["financial"]["dictionary_entries"], int)
    assert isinstance(opera["financial"]["by_confidence"], dict)
    assert isinstance(opera["segments"]["unmapped_codes"], list)
    assert isinstance(opera["statistics"]["unmapped_labels"], list)
    _assert_no_floats(body)
    # Nonsense editions are rejected by validation, not silently empty.
    assert client.get("/api/coverage", params={"edition": 0}).status_code == 422
    assert client.get("/api/coverage", params={"edition": -1}).status_code == 422


def test_ingest_endpoint_still_mounted(client):
    # Task 2 must not disturb POST /ingest; a garbage upload still 422s with detail.
    r = client.post("/ingest", files={"file": ("junk.pdf", b"%PDF-1.4 garbage", "application/pdf")})
    assert r.status_code == 422
    assert "junk.pdf" in r.json()["detail"]


def _make_spa_client(db_engine: Engine, tmp_path: Path, dist_dir: Path) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        dist_dir=dist_dir,
        token_verifier=_VERIFIER,
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    return client


def test_spa_mounted_when_dist_exists(db_engine, db_session, founding_org, tmp_path):
    # With a built frontend present, GET / serves index.html — but the API and
    # ingest routes still win because the SPA mount is registered last.
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>usali portal</body></html>")
    (dist / "app.js").write_text("console.log('bundle')")
    client = _make_spa_client(db_engine, tmp_path, dist)
    r = client.get("/")
    assert r.status_code == 200 and "usali portal" in r.text
    # SPA deep links (client-side routes with no file on disk) fall back to
    # index.html so refresh/bookmarks work when `usali serve` hosts the build.
    for deep_link in ("/coverage", "/upload", "/coverage/nested/thing"):
        r = client.get(deep_link)
        assert r.status_code == 200 and "usali portal" in r.text, deep_link
    # Real static assets are still served as themselves, not index.html.
    r = client.get("/app.js")
    assert r.status_code == 200 and "bundle" in r.text
    assert client.get("/api/properties").status_code == 200
    # Unknown API paths are real 404s — the fallback never shadows /api/*.
    assert client.get("/api/nope").status_code == 404


def test_missing_file_paths_404_instead_of_returning_the_spa(
    db_engine, db_session, founding_org, tmp_path
):
    """A request that NAMES A FILE and finds none is a 404, not the SPA.

    Serving index.html with a 200 for `/wp-config.php` is not a vulnerability,
    but it is a lie: every automated scanner records the probe as a hit, so a
    scan report reads as a list of successful PHP exploits against a site that
    runs no PHP. Observed live on 2026-08-26 — `/wp-content/plugins/hellopress/
    wp_filemanager.php` and `/this_is_a_new_hello_world.php` both came back 200.

    The rule is a dot in the LAST path segment. Client-side routes never have
    one (`/coverage`, `/night-audit`, `/kiosk-devices`), and real assets like
    `/assets/index-abc.js` exist on disk so they never reach the fallback at
    all. A future route with a dot in it would 404 on refresh — hence this test,
    which says why the constraint exists.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>usali portal</body></html>")
    (dist / "app.js").write_text("console.log('bundle')")
    client = _make_spa_client(db_engine, tmp_path, dist)

    for probe in (
        "/wp-content/plugins/hellopress/wp_filemanager.php",
        "/this_is_a_new_hello_world.php",
        "/vpn.php",
        "/.env",
        "/backup.sql",
        # Dot-prefixed DIRECTORY, ordinary filename. The first implementation
        # checked only the last segment and let these through — and `.git/HEAD`
        # is among the most-probed paths on the internet.
        "/.git/HEAD",
        "/.ssh/id_rsa",
        "/.aws/credentials",
        "/nested/deep/.git/HEAD",
    ):
        assert client.get(probe).status_code == 404, probe

    # The fallback still does its actual job for client-side routes...
    for deep_link in ("/coverage", "/night-audit", "/kiosk-devices", "/coverage/nested"):
        r = client.get(deep_link)
        assert r.status_code == 200 and "usali portal" in r.text, deep_link
    # ...and a real asset that EXISTS is still served as itself.
    assert "bundle" in client.get("/app.js").text
    r = client.post("/ingest", files={"file": ("junk.pdf", b"garbage", "application/pdf")})
    assert r.status_code == 422  # routed to the ingest handler, not the SPA


def test_api_only_without_dist(db_engine, db_session, founding_org, tmp_path):
    # No frontend build -> API-only app; GET / is a plain 404, /api still works.
    client = _make_spa_client(db_engine, tmp_path, tmp_path / "missing-dist")
    assert client.get("/").status_code == 404
    assert client.get("/api/properties").status_code == 200
