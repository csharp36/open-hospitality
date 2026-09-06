"""The /api/gl surface (OH-27 Task 11): org_admin gates on every
mutation, close names the gaps in both directions, and the trial balance
mirrors `reporting.trial_balance` field-for-field with Decimals as
strings.

Auth follows the mount: reads ride `create_app`'s operator gates (any
operator role may look at the chart and the periods), while mutations
narrow to org_admin through the router's own `require_gl_admin` — the
`integrations_api` split, tested the same way: an org_admin client, a
property_gm client for the 403s, and an employee token for the outer
operator gate.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.grants import grant_role
from tests.test_gl_posting import _first_grain, _seed_calendar
from usali import gl_chart, gl_posting, reporting
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import GlAccount, UsaliFinancialFact
from usali.server import create_app


def _client(db_engine, tmp_path) -> tuple[TestClient, object]:
    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app), mint


def _authenticated(client, mint, db_session, role: str, sub: str) -> TestClient:
    # Both halves of the L4 gate: the realm's coarse claim on the token AND
    # the org-scoped `role_assignment` grant `require_grants` reads.
    grant_role(db_session, role, sub=sub, org_id=1)
    client.headers["Authorization"] = f"Bearer {mint(roles=[role], sub=sub)}"
    return client


@pytest.fixture
def gl_client(db_engine, db_session, founding_org, tmp_path) -> TestClient:
    client, mint = _client(db_engine, tmp_path)
    return _authenticated(client, mint, db_session, "org_admin", "gl-admin")


@pytest.fixture
def gl_client_gm(db_engine, db_session, founding_org, tmp_path) -> TestClient:
    """A property_gm — passes the mount's operator gate, holds no org_admin
    grant. The strongest caller every mutation must still refuse."""
    client, mint = _client(db_engine, tmp_path)
    return _authenticated(client, mint, db_session, "property_gm", "gl-gm")


@pytest.fixture
def gl_client_employee(db_engine, db_session, founding_org, tmp_path) -> TestClient:
    """An employee token: not an operator, so the MOUNT's gate refuses it
    before any handler — the reads included."""
    client, mint = _client(db_engine, tmp_path)
    client.headers["Authorization"] = f"Bearer {mint(roles=['employee'], sub='gl-emp')}"
    return client


@pytest.fixture
def gl_world(db_session, founding_org, seed_six_pdfs):
    """Chart + calendar over the six-PDF facts: the state every GL endpoint
    assumes. Committed, because the API reads on ANOTHER connection."""
    gl_chart.seed_chart(db_session, org_id=1)
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    db_session.commit()
    return prop, day


def _post_range(client: TestClient, prop, day_from, day_to=None):
    resp = client.post(
        "/api/gl/post",
        json={
            "property_id": prop,
            "date_from": day_from.isoformat(),
            "date_to": (day_to or day_from).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ----------------------------------------------------------------- the chart


def test_accounts_get_lists_the_seeded_chart(gl_client, gl_world):
    resp = gl_client.get("/api/gl/accounts")
    assert resp.status_code == 200
    by_code = {row["account_code"]: row for row in resp.json()}
    assert by_code["4000"]["account_type"] == "income"
    assert by_code["1210"]["system_role"] == "guest_ledger_clearing"
    assert all(row["is_active"] for row in by_code.values())


def test_role_bearing_accounts_refuse_deactivation(gl_client, gl_world, db_session):
    """The D-OH27.2 enforcement point: the engine finds structural accounts
    by system_role, so deactivating the carrier is refused BY NAME."""
    resp = gl_client.put(
        "/api/gl/accounts/1210",
        json={"name": "Guest Ledger", "account_type": "asset", "is_active": False},
    )
    assert resp.status_code == 422
    assert "guest_ledger_clearing" in resp.json()["detail"]
    db_session.expire_all()
    row = db_session.scalar(
        select(GlAccount).where(GlAccount.account_code == "1210")
    )
    assert row.is_active is True  # the refusal left the row standing


def test_accounts_put_creates_an_org_extension_row(gl_client, gl_world, db_session):
    resp = gl_client.put(
        "/api/gl/accounts/4900",
        json={"name": "Marina Income", "account_type": "income"},
    )
    assert resp.status_code == 204
    db_session.expire_all()
    row = db_session.scalar(
        select(GlAccount).where(GlAccount.account_code == "4900")
    )
    assert row is not None
    assert (row.name, row.account_type, row.is_active) == ("Marina Income", "income", True)
    assert row.system_role is None


def test_accounts_put_refuses_an_unknown_account_type(gl_client, gl_world, db_session):
    resp = gl_client.put(
        "/api/gl/accounts/4901",
        json={"name": "Typo", "account_type": "revenue"},
    )
    assert resp.status_code == 422
    assert "account_type" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.scalar(
        select(GlAccount).where(GlAccount.account_code == "4901")
    ) is None


# ------------------------------------------------------------------- periods


def test_close_returns_gaps_and_reopen_requires_reason(gl_client, gl_world, db_session):
    prop, day = gl_world
    period = gl_posting.period_key_for(db_session, prop, day)

    resp = gl_client.put(f"/api/gl/periods/{period}/close", params={"property": prop})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "closed"
    assert day.isoformat() in body["unposted_dates"]  # nothing was posted yet
    assert body["orphaned_dates"] == []

    listed = gl_client.get(
        "/api/gl/periods", params={"property": prop, "fiscal_year": day.year}
    )
    assert listed.status_code == 200
    states = {p["period_key"]: p["state"] for p in listed.json()}
    assert states[period] == "closed"

    resp = gl_client.put(
        f"/api/gl/periods/{period}/reopen",
        params={"property": prop}, json={"reason": ""},
    )
    assert resp.status_code == 422
    assert "reason" in resp.json()["detail"]

    resp = gl_client.put(
        f"/api/gl/periods/{period}/reopen",
        params={"property": prop}, json={"reason": "late audit pack"},
    )
    assert resp.status_code == 204
    db_session.expire_all()
    assert gl_posting.period_state(db_session, prop, period) == "open"


# ------------------------------------------------------------------- posting


def test_post_endpoint_reports_outcomes(gl_client, gl_world):
    prop, day = gl_world
    rows = _post_range(gl_client, prop, day)
    by_source = {row["source_type"]: row for row in rows}
    assert set(by_source) == {s.source_type for s in gl_posting.POSTING_SOURCES}
    assert all(row["business_date"] == day.isoformat() for row in rows)
    assert by_source["pms_daily"]["status"] == "posted"
    # No labor facts were seeded, and no entry stands: skipped, not failed.
    assert by_source["payroll_accrual"]["status"] == "skipped"


def test_post_without_a_chart_reports_honest_skips(
    gl_client, db_session, founding_org, seed_six_pdfs
):
    """No chart is not an API error: every grain comes back "skipped" —
    the same answer `post_and_record` gives the CLI and the ingestion hook.
    The calendar IS seeded: without one the endpoint's up-front gate is a
    422 before any grain is tried (test_post_refuses_a_property_it_cannot_post pins that)."""
    prop, day = _first_grain(db_session)
    _seed_calendar(db_session, prop)
    db_session.commit()
    rows = _post_range(gl_client, prop, day, day + timedelta(days=1))
    assert len(rows) == 2 * len(gl_posting.POSTING_SOURCES)
    assert {row["status"] for row in rows} == {"skipped"}


def test_post_reports_reversed_when_the_facts_are_gone(
    gl_client, gl_world, db_session
):
    """The Task 8 flow through the API: a posted grain whose facts were
    deleted re-posts as a reversal, and the outcome says so."""
    prop, day = gl_world
    first = _post_range(gl_client, prop, day)
    assert {r["source_type"]: r["status"] for r in first}["pms_daily"] == "posted"

    db_session.execute(
        UsaliFinancialFact.__table__.delete().where(
            UsaliFinancialFact.property_id == prop,
            UsaliFinancialFact.business_date == day,
        )
    )
    db_session.commit()  # the API posts on ANOTHER connection

    second = _post_range(gl_client, prop, day)
    assert {r["source_type"]: r["status"] for r in second}["pms_daily"] == "reversed"

    # The reversal returned the grain to unposted: a third post over the
    # still-empty day is an ordinary skip, not a second reversal.
    third = _post_range(gl_client, prop, day)
    assert {r["source_type"]: r["status"] for r in third}["pms_daily"] == "skipped"


def test_post_refuses_a_property_it_cannot_post(gl_client, gl_world):
    """A typo'd property is a 422, not a stack trace: the endpoint's
    up-front gate is one `period_key_for` call, so a nonexistent property
    (which can never have a FiscalCalendar row) and a real-but-calendarless
    property get the SAME refusal — deliberately indistinguishable, since
    both mean "not postable" and telling them apart would say which
    property ids exist. Without the gate, `post_and_record`'s failure
    ledger would insert a row for the bogus property and 500 on its
    property FK."""
    _prop, day = gl_world
    resp = gl_client.post(
        "/api/gl/post",
        json={
            "property_id": "NOPE",
            "date_from": day.isoformat(),
            "date_to": day.isoformat(),
        },
    )
    assert resp.status_code == 422, resp.text
    assert "fiscal calendar" in resp.json()["detail"]


def test_post_caps_the_date_range(gl_client, gl_world):
    prop, day = gl_world
    resp = gl_client.post(
        "/api/gl/post",
        json={
            "property_id": prop,
            "date_from": day.isoformat(),
            "date_to": (day + timedelta(days=401)).isoformat(),
        },
    )
    assert resp.status_code == 422
    assert "400 days" in resp.json()["detail"]
    assert "gl-post" in resp.json()["detail"]  # the CLI is the backfill path


# ------------------------------------------------------------- trial balance


def test_trial_balance_endpoint_mirrors_reporting(gl_client, gl_world, db_session):
    prop, day = gl_world
    _post_range(gl_client, prop, day)
    period = gl_posting.period_key_for(db_session, prop, day)

    resp = gl_client.get(
        "/api/gl/trial-balance", params={"property": prop, "period": period}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    db_session.expire_all()
    report = reporting.trial_balance(db_session, property_id=prop, period_key=period)
    assert body["property_id"] == report.property_id
    assert body["period_key"] == report.period_key
    assert body["date_from"] == report.date_from.isoformat()
    assert body["date_to"] == report.date_to.isoformat()
    assert body["total_debits"] == str(report.total_debits)
    assert body["total_credits"] == str(report.total_credits)
    assert Decimal(body["total_debits"]) == Decimal(body["total_credits"])
    assert body["lines"] == [
        {
            "account_code": line.account_code,
            "name": line.name,
            "account_type": line.account_type,
            "debits": str(line.debits),
            "credits": str(line.credits),
        }
        for line in report.lines
    ]


def test_trial_balance_is_404_on_an_empty_period(gl_client, gl_world):
    prop, _day = gl_world
    resp = gl_client.get(
        "/api/gl/trial-balance", params={"property": prop, "period": "2019-P01"}
    )
    assert resp.status_code == 404


# ------------------------------------------------------- the authorization


def test_reads_ride_the_operator_gate(gl_client_gm, gl_client_employee, gl_world):
    """The mount decides the reads: any operator may look, an employee
    token may not reach any handler at all."""
    prop, _day = gl_world
    assert gl_client_gm.get("/api/gl/accounts").status_code == 200
    assert gl_client_employee.get("/api/gl/accounts").status_code == 403
    assert gl_client_employee.get(
        "/api/gl/periods", params={"property": prop}
    ).status_code == 403


def test_every_mutation_requires_org_admin(gl_client_gm, gl_world):
    prop, day = gl_world
    assert gl_client_gm.put(
        "/api/gl/accounts/4000",
        json={"name": "Rooms", "account_type": "income"},
    ).status_code == 403
    assert gl_client_gm.put(
        "/api/gl/periods/2026-P01/close", params={"property": prop}
    ).status_code == 403
    assert gl_client_gm.put(
        "/api/gl/periods/2026-P01/reopen",
        params={"property": prop}, json={"reason": "x"},
    ).status_code == 403
    assert gl_client_gm.post(
        "/api/gl/post",
        json={
            "property_id": prop,
            "date_from": day.isoformat(),
            "date_to": day.isoformat(),
        },
    ).status_code == 403
