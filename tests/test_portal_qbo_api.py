"""Portal API for the CPA pack and QBO push (P8 Task 6).

Endpoint tests over the seeded six-PDF database with the QBO side served by
the in-process mock through ``SyncASGITransport`` — the portal's client factory
is injected, so the real shared-client wiring in ``create_app`` is exercised
end to end (two pushes through the SAME app prove the rotated refresh token
survives across requests; a per-request client would invalid_grant on the
second push). Also: the CPA pack's explicit error mapping (unknown property is
a plain ValueError, NOT a NoFactsError — mapped to 404 by message), the
structured unmapped-GL 422 worklist, the 502 wrap for an unreachable QBO, and
the no-float walker over every new payload.
"""

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.models import UsaliFinancialFact
from usali.qbo_client import QboClient, StaticTokenStore, SyncASGITransport
from usali.qbo_mock import create_mock_qbo
from usali.server import create_app

_VERIFIER, _MINT = make_authkit()

BASIC_AUTH = {"Authorization": "Basic Y2xpZW50OnNlY3JldA=="}  # client:secret
REALM = "mock-realm"
DAY = "2026-07-07"
MONTH = "2026-07"


def _bootstrap_refresh_token(mock_app: Any) -> str:
    """authorization_code grant — the test stand-in for Intuit's consent flow."""
    with httpx.Client(transport=SyncASGITransport(mock_app), base_url="http://mock-qbo") as c:
        resp = c.post(
            "/oauth2/v1/tokens/bearer",
            data={"grant_type": "authorization_code", "code": "bootstrap"},
            headers=BASIC_AUTH,
        )
        assert resp.status_code == 200, resp.text
        token: str = resp.json()["refresh_token"]
        return token


def _stored_jes(mock_app: Any) -> list[dict[str, Any]]:
    with httpx.Client(transport=SyncASGITransport(mock_app), base_url="http://mock-qbo") as c:
        entries: list[dict[str, Any]] = c.get("/mock/journalentries").json()
        return entries


def _make_client(
    db_engine: Engine, tmp_path: Path, qbo_factory: Any = None
) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        qbo_client_factory=qbo_factory,
        token_verifier=_VERIFIER,
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    return client


@pytest.fixture
def mock_app() -> Any:
    return create_mock_qbo()


@pytest.fixture
def client(
    db_engine: Engine, db_session: Session, seed_six_pdfs: None,
    tmp_path: Path, mock_app: Any
) -> TestClient:
    """Portal app whose QBO client factory targets the in-process mock.

    The refresh token is bootstrapped exactly ONCE, outside the factory, and
    every factory call closes over that single token. That is what makes the
    rotation test a real regression guard: the first client's refresh consumes
    (rotates) the token, so if `create_app` called the factory per request
    instead of sharing one client, the second factory-built client would
    refresh with the already-consumed token -> invalid_grant -> failed push.
    (Bootstrapping INSIDE the factory would mint every client its own fresh
    token — the mock's authorization_code grant is repeatable — and mask
    exactly that regression.) `factory_calls` counts invocations so the
    multi-push test can also assert the factory ran exactly once.
    """
    token = _bootstrap_refresh_token(mock_app)
    factory_calls: list[None] = []

    def factory() -> QboClient:
        factory_calls.append(None)
        return QboClient(
            "http://mock-qbo",
            "client",
            "secret",
            REALM,
            StaticTokenStore(token),
            transport=SyncASGITransport(mock_app),
        )

    # L4: the minted accountant's authority is its org-wide DB grant.
    grant_role(db_session, "accountant")
    test_client = _make_client(db_engine, tmp_path, factory)
    test_client.qbo_factory_calls = factory_calls  # type: ignore[attr-defined]
    return test_client


def _assert_no_floats(value: object, path: str = "$") -> None:
    assert not isinstance(value, float), f"float leaked into payload at {path}: {value!r}"
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_floats(item, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _assert_no_floats(item, f"{path}[{i}]")


def _null_parking_gl(session: Session) -> None:
    """Un-map HISJ's Parking fact — the unmapped-GL worklist scenario."""
    session.execute(
        update(UsaliFinancialFact)
        .where(
            UsaliFinancialFact.property_id == "HISJ",
            UsaliFinancialFact.usali_line_item == "Parking",
        )
        .values(gl_account_code=None)
    )
    session.commit()


# --- GET /api/cpa-pack --------------------------------------------------------------


def test_cpa_pack_shape_and_values(client):
    r = client.get("/api/cpa-pack", params={"property": "HISJ", "month": MONTH})
    assert r.status_code == 200, r.text
    pack = r.json()
    assert pack["property_id"] == "HISJ"
    assert pack["pms_source"] == "OPERA"
    assert pack["month"] == MONTH

    sales = pack["sales"]
    assert Decimal(sales["total_operating_revenue"]) == Decimal("10866.37")
    assert sales["lines"]
    assert sum(Decimal(li["mtd_amount"]) for li in sales["lines"]) == Decimal("10866.37")
    assert all(li["day_count"] == 1 for li in sales["lines"])  # single seeded date

    taxes = pack["taxes"]
    assert Decimal(taxes["taxes_total"]) == Decimal("1573.29")
    assert Decimal(taxes["room_revenue_base"]) == Decimal("10395.00")
    assert all(li["gl_account_code"] == "2100" for li in taxes["lines"])

    by_code = {li["ledger_code"]: li for li in pack["ar"]["balances"]}
    guest = by_code["GUEST_LEDGER"]
    assert guest["ledger_name"] == "Guest Ledger"
    # Opening is the FIRST reported balance IN the month (per the ArLine note);
    # with a single seeded date it equals the close and the movement is zero.
    assert Decimal(guest["opening_balance"]) == Decimal("11627.58")
    assert Decimal(guest["closing_balance"]) == Decimal("11627.58")
    assert Decimal(guest["movement"]) == 0

    _assert_no_floats(pack)


def test_cpa_pack_empty_month_is_clean_not_error(client):
    # cpa_pack's contract: an empty month for a KNOWN property is a clean empty
    # pack (the source falls back to a property-wide lookup), never a 404.
    r = client.get("/api/cpa-pack", params={"property": "SSSJ", "month": "2026-06"})
    assert r.status_code == 200, r.text
    pack = r.json()
    assert pack["pms_source"] == "AUTOCLERK"
    assert pack["sales"]["lines"] == [] and pack["ar"]["balances"] == []
    assert Decimal(pack["sales"]["total_operating_revenue"]) == 0


def test_cpa_pack_unknown_property_is_404(client):
    # "unknown property" is a plain ValueError from cpa_pack, NOT a NoFactsError;
    # the endpoint maps it to 404 explicitly (a missing resource, not a bad request).
    r = client.get("/api/cpa-pack", params={"property": "NOPE", "month": MONTH})
    assert r.status_code == 404, r.text
    assert "unknown property" in r.json()["detail"]


def test_cpa_pack_bad_month_is_422(client):
    for bad in ("2026-13", "not-a-month", "2026-7"):
        r = client.get("/api/cpa-pack", params={"property": "HISJ", "month": bad})
        assert r.status_code == 422, (bad, r.text)
        assert "month" in r.json()["detail"]
    # Missing params -> FastAPI validation.
    assert client.get("/api/cpa-pack", params={"property": "HISJ"}).status_code == 422
    assert client.get("/api/cpa-pack", params={"month": MONTH}).status_code == 422


# --- GET /api/qbo/preview -----------------------------------------------------------


def test_qbo_preview_balanced_plan(client):
    r = client.get("/api/qbo/preview", params={"property": "HISJ", "date": DAY})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["property_id"] == "HISJ"
    assert plan["business_date"] == DAY
    assert Decimal(plan["total_debits"]) == Decimal("12439.66")
    assert Decimal(plan["total_credits"]) == Decimal("12439.66")
    assert len(plan["request_hash"]) == 64
    assert len(plan["lines"]) == 7
    debits = sum(
        Decimal(li["amount"]) for li in plan["lines"] if li["posting"] == "Debit"
    )
    credits = sum(
        Decimal(li["amount"]) for li in plan["lines"] if li["posting"] == "Credit"
    )
    assert debits == credits == Decimal("12439.66")
    assert all(
        li["account_name"] and li["memo"] and Decimal(li["amount"]) > 0
        for li in plan["lines"]
    )
    _assert_no_floats(plan)


def test_qbo_preview_unmapped_gl_is_structured_422(client, db_session):
    _null_parking_gl(db_session)
    r = client.get("/api/qbo/preview", params={"property": "HISJ", "date": DAY})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == {
        "unmapped": [
            {
                "major": "Miscellaneous Income",
                "sub_category": "Parking",
                "line_item": "Parking",
            }
        ]
    }


def test_qbo_preview_no_facts_is_404_and_bad_date_is_422(client):
    r = client.get("/api/qbo/preview", params={"property": "HISJ", "date": "2026-01-01"})
    assert r.status_code == 404 and "no financial facts" in r.json()["detail"]
    r = client.get("/api/qbo/preview", params={"property": "HISJ", "date": "not-a-date"})
    assert r.status_code == 422


# --- POST /api/qbo/push + GET /api/qbo/status ---------------------------------------


def test_push_status_repush_and_shared_client_rotation(client, mock_app):
    # First write action of the portal: push HISJ.
    r = client.post("/api/qbo/push", json={"property": "HISJ", "date": DAY})
    assert r.status_code == 200, r.text
    result = r.json()
    assert result == {"status": "pushed", "qbo_je_id": "1", "message": None}
    _assert_no_floats(result)
    assert len(_stored_jes(mock_app)) == 1

    # Re-push of identical content is an idempotent no-op.
    r = client.post("/api/qbo/push", json={"property": "HISJ", "date": DAY})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "already-pushed", "qbo_je_id": "1", "message": None}
    assert len(_stored_jes(mock_app)) == 1

    # A second push through the SAME app (the seeded corpus has one business
    # date, so a different PROPERTY) proves the shared client survives refresh-
    # token rotation: the fixture's single bootstrap token was consumed by the
    # first push's refresh, so a per-request client would invalid_grant here.
    r = client.post("/api/qbo/push", json={"property": "SSSJ", "date": DAY})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "pushed", "qbo_je_id": "2", "message": None}
    assert len(_stored_jes(mock_app)) == 2
    # Belt and braces: three pushes, ONE factory call — the app built exactly
    # one client and shared it.
    assert len(client.qbo_factory_calls) == 1

    # The push ledger over the API, unfiltered and filtered.
    r = client.get("/api/qbo/status")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(row["property_id"], row["business_date"], row["status"]) for row in rows] == [
        ("HISJ", DAY, "pushed"),
        ("SSSJ", DAY, "pushed"),
    ]
    assert all(
        row["qbo_je_id"] and len(row["request_hash"]) == 64 and row["message"] is None
        and row["pushed_at"]
        for row in rows
    )
    _assert_no_floats(rows)

    r = client.get("/api/qbo/status", params={"property": "SSSJ"})
    assert [row["property_id"] for row in r.json()] == ["SSSJ"]
    r = client.get("/api/qbo/status", params={"property": "HISJ", "month": MONTH})
    assert len(r.json()) == 1
    assert client.get("/api/qbo/status", params={"month": "2026-06"}).json() == []


def test_qbo_status_bad_month_is_422(client):
    r = client.get("/api/qbo/status", params={"month": "not-a-month"})
    assert r.status_code == 422 and "month" in r.json()["detail"]


def test_push_unmapped_gl_is_structured_422(client, db_session, mock_app):
    _null_parking_gl(db_session)
    r = client.post("/api/qbo/push", json={"property": "HISJ", "date": DAY})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["unmapped"] == [
        {"major": "Miscellaneous Income", "sub_category": "Parking", "line_item": "Parking"}
    ]
    assert _stored_jes(mock_app) == []
    assert client.get("/api/qbo/status").json() == []


def test_push_no_facts_is_404_and_bad_body_is_422(client):
    r = client.post("/api/qbo/push", json={"property": "HISJ", "date": "2026-01-01"})
    assert r.status_code == 404 and "no financial facts" in r.json()["detail"]
    assert client.post("/api/qbo/push", json={"property": "HISJ"}).status_code == 422
    assert (
        client.post("/api/qbo/push", json={"property": "HISJ", "date": "junk"}).status_code
        == 422
    )


def test_push_unreachable_qbo_is_502(db_engine, db_session, seed_six_pdfs, tmp_path):
    # Nothing listens on port 1: the transport error is wrapped as a clear 502,
    # not a raw httpx traceback (a 500), and nothing is recorded in the ledger.
    def factory() -> QboClient:
        return QboClient(
            "http://127.0.0.1:1", "client", "secret", REALM, StaticTokenStore("token")
        )

    grant_role(db_session, "accountant")
    client = _make_client(db_engine, tmp_path, factory)
    r = client.post("/api/qbo/push", json={"property": "HISJ", "date": DAY})
    assert r.status_code == 502, r.text
    assert "cannot reach QBO" in r.json()["detail"]
    assert client.get("/api/qbo/status").json() == []


def test_default_qbo_factory_builds_lazily(db_engine, db_session, founding_org, tmp_path):
    # Without an injected factory the app must not touch the network or settings
    # eagerly: read-only endpoints work with no QBO reachable anywhere.
    client = _make_client(db_engine, tmp_path, None)
    assert client.get("/api/properties").status_code == 200
    assert client.get("/api/qbo/status").json() == []
