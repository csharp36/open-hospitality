"""Portal API for the CPA pack and QBO push (P8 Task 6).

Endpoint tests over the seeded six-PDF database with the QBO side served by
the in-process mock through ``SyncASGITransport`` — the portal's client factory
is injected, so the real per-request wiring in ``create_app`` is exercised end
to end (three pushes through the SAME app build THREE clients over ONE token
store, and prove the rotated refresh token survives across them).

That used to read the other way round: before OH-17 ``create_app`` memoized one
shared client for the app's lifetime because the rotated token lived in client
MEMORY, so a per-request client would ``invalid_grant`` on the second push.
``DbTokenStore`` moved the lineage onto the tenant's credential row, which is
what let the memoizer go — and had to go, because one process-wide client is
one tenant's connection handed to every tenant.

Also: the CPA pack's explicit error mapping (unknown property is a plain
ValueError, NOT a NoFactsError — mapped to 404 by message), the structured
unmapped-GL 422 worklist, the 502 wrap for an unreachable QBO, the loud
refusal when the tenant has no accounting credential, and the no-float walker
over every new payload.
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
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS
from usali.models import Organization, Property, UsaliFinancialFact
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

    The refresh token is bootstrapped exactly ONCE, outside the factory, into
    ONE `StaticTokenStore` that every client the factory builds SHARES. That
    store is the test's stand-in for `integrations.DbTokenStore`: a durable
    per-tenant lineage that outlives any single client, which is precisely
    what OH-17 put on the credential row.

    Sharing the STORE (and not the client) is what keeps the rotation test a
    real regression guard. The first push's refresh consumes and rotates the
    bootstrap token and writes the replacement back to the store; the second
    request builds a NEW client, loads the rotated token from that same store,
    and succeeds. Give each client its own `StaticTokenStore(token)` instead
    and the second push `invalid_grant`s — that failure is the whole point of
    the port, so do not "simplify" this back into a per-client store.
    (Bootstrapping INSIDE the factory would mint every client its own fresh
    token — the mock's authorization_code grant is repeatable — and mask the
    regression just as thoroughly.) `factory_calls` counts invocations so the
    multi-push test can assert the factory now runs once PER REQUEST.
    """
    store = StaticTokenStore(_bootstrap_refresh_token(mock_app))
    factory_calls: list[None] = []

    def factory(_session_factory: Any = None) -> QboClient:
        factory_calls.append(None)
        return QboClient(
            "http://mock-qbo",
            "client",
            "secret",
            REALM,
            store,
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


def test_push_status_repush_and_rotation_across_per_request_clients(client, mock_app):
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
    # date, so a different PROPERTY) proves the rotated refresh token survives
    # ACROSS CLIENTS: the fixture's single bootstrap token was consumed by the
    # first push's refresh, and this request is served by a brand-new client
    # that can only succeed by loading the rotation from the shared store.
    r = client.post("/api/qbo/push", json={"property": "SSSJ", "date": DAY})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "pushed", "qbo_je_id": "2", "message": None}
    assert len(_stored_jes(mock_app)) == 2
    # Belt and braces: three pushes, THREE factory calls — OH-17 deleted the
    # `_shared` memoizer, so each request resolves its own client from the
    # ACTIVE ORG's credential row. One process-wide client would be one
    # tenant's QBO connection serving every tenant.
    assert len(client.qbo_factory_calls) == 3

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
    def factory(_session_factory: Any = None) -> QboClient:
        return QboClient(
            "http://127.0.0.1:1", "client", "secret", REALM, StaticTokenStore("token")
        )

    grant_role(db_session, "accountant")
    client = _make_client(db_engine, tmp_path, factory)
    r = client.post("/api/qbo/push", json={"property": "HISJ", "date": DAY})
    assert r.status_code == 502, r.text
    assert "cannot reach QBO" in r.json()["detail"]
    assert client.get("/api/qbo/status").json() == []


def test_an_unconnected_tenant_gets_a_503_naming_the_connect_surface(
    db_engine, db_session, tmp_path
):
    """ADR-010: no accounting credential row => a loud, named refusal. It must
    NOT fall back to the process-wide `USALI_QBO_*` env, which still holds a
    working local-mock realm and token — a process-wide credential is not this
    tenant's connection.

    The world is built by hand rather than via `seed_six_pdfs`, because
    `seed_properties` -> `ensure_default_org` PLANTS org 1's accounting row
    from env (D-OH17.15). A test that used the seeded world would resolve a
    real client and never reach the refusal — green, and proving nothing.

    HISJ here has no financial facts either, so the 503 also pins the ORDER:
    the tenant is told it is not connected before the push is attempted and
    404s on empty facts. (The other edge of that order — a 403 for an
    out-of-scope property, which must still beat the 503 — is pinned in
    tests/test_workforce_api.py::test_qbo_push_property_scope_enforced.)"""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS,
                                name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.commit()
    grant_role(db_session, "accountant")

    client = _make_client(db_engine, tmp_path, None)
    r = client.post("/api/qbo/push", json={"property": "HISJ", "date": DAY})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "/integrations" in detail
    assert "USALI_QBO" not in detail


def test_default_qbo_factory_builds_lazily(db_engine, db_session, founding_org, tmp_path):
    # Without an injected factory the app must not touch the network or settings
    # eagerly: read-only endpoints work with no QBO reachable anywhere.
    client = _make_client(db_engine, tmp_path, None)
    assert client.get("/api/properties").status_code == 200
    assert client.get("/api/qbo/status").json() == []
