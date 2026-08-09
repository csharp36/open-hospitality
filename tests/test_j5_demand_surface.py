"""J5 backend: the demand read surface (plan decision 4).

GET /api/crm/demand serves the LATEST snapshot per stay-date to the
scheduler surfaces (forecast hints + builder chip), capability-labeled
so a dimension the provider does not speak renders as ABSENT, never as
a silent 0 (the D2 forecast-null rule). Demand INFORMS the forecast; it
never becomes it — this endpoint returns no targets, no auto-fill, and
the kiosk never sees any of it (the my-week payload pin lives in
tests/test_kiosk_my_week.py).

Feature-off DEGRADES here (configured: false, no days) rather than
refusing: a read surface with nothing to say is an honest gap — unlike
the PULL, where a silent no-op would eat an operator's explicit act
(J4's loud 503)."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.config import get_settings
from usali.crm_feed import CrmCapabilities, CrmDemandDay, CrmDemandPull, InMemoryCrmFeed
from usali.crm_pull import store_pull
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import AuditEvent, Organization, OrgSettings, Property
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.flush()  # org row before its org_settings FK child
    # L5: the provider lives on org 1's org_settings row, seeded from env.
    db_session.add(OrgSettings(org_id=1, crm_provider=get_settings().crm_provider))
    db_session.add_all([
        Property(property_id="HISJ", org_id=1, name="HISJ",
                 pms_source="OPERA", wage_jurisdiction="US-CA",
                 crm_ref="REF-1"),
        Property(property_id="SSSJ", org_id=1, name="SSSJ",
                 pms_source="OPERA", wage_jurisdiction="US-CA"),
    ])
    db_session.commit()
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "accountant", sub="a")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    grant_role(db_session, "property_gm", sub="gm2", property_id="SSSJ")


def _client(db_engine, tmp_path, verifier, feed):
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
        crm_feed_factory=lambda provider: feed if provider else None,
    )
    return TestClient(app)


def _admin(mint):
    return {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}


def _gm(mint, prop="HISJ", sub="gm"):
    tok = mint(roles=["property_gm"], sub=sub,
               scopes=[{"property_id": prop, "department_id": None}])
    return {"Authorization": f"Bearer {tok}"}


_DELPHI_CAPS = CrmCapabilities(emits_rooms_on_books=True,
                               emits_group_rooms=True)

_WINDOW = {"property": "HISJ", "start": "2026-08-01", "end": "2026-08-31"}


def _store(session, days, property_id="HISJ", horizon=None):
    """`horizon` defaults to the full August window; pass a narrower one
    to model a pull that did not cover a date (a batch's silence WITHIN
    its horizon is a cancellation, not a gap — the J7 rule)."""
    start, end = horizon or (date(2026, 8, 1), date(2026, 8, 31))
    return store_pull(
        session, property_id=property_id, provider="delphi",
        horizon_start=start, horizon_end=end,
        pull=CrmDemandPull(days=tuple(days)),
    )


@pytest.fixture
def crm_on(monkeypatch):
    monkeypatch.setenv("USALI_CRM_PROVIDER", "delphi")


def test_demand_renders_from_the_latest_snapshot(
    db_engine, db_session, tmp_path, crm_on
):
    """The surface reads the newest voice per stay-date, carries the
    as-of stamp per day, and labels ride along — this IS a scheduler
    surface, the one place labels belong."""
    _seed(db_session)
    _store(db_session, [
        CrmDemandDay(stay_date=date(2026, 8, 6), rooms_on_books=120,
                     group_rooms=40, event_covers=None,
                     labels=("Acme Corp Annual",)),
        CrmDemandDay(stay_date=date(2026, 8, 7), rooms_on_books=110,
                     group_rooms=30, event_covers=None),
    ])
    # The newer pull covered ONLY Aug 6 — Aug 7 keeps its older voice
    # because the newer horizon never reached it (had it covered Aug 7
    # and said nothing, the day would be a cancellation and drop out).
    _store(db_session, [
        CrmDemandDay(stay_date=date(2026, 8, 6), rooms_on_books=132,
                     group_rooms=50, event_covers=None,
                     labels=("Acme Corp Annual", "Delta Sigma Reunion")),
    ], horizon=(date(2026, 8, 6), date(2026, 8, 6)))
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier,
                InMemoryCrmFeed(feed_capabilities=_DELPHI_CAPS))
    r = c.get("/api/crm/demand", headers=_gm(mint), params=_WINDOW)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["provider"] == "delphi"
    assert body["capabilities"] == {
        "rooms_on_books": True, "group_rooms": True, "event_covers": False,
    }
    assert [(d["stay_date"], d["rooms_on_books"], d["group_rooms"])
            for d in body["days"]] == [
        ("2026-08-06", 132, 50),  # the newer voice
        ("2026-08-07", 110, 30),  # the only voice
    ]
    assert body["days"][0]["labels"] == "Acme Corp Annual, Delta Sigma Reunion"
    assert body["days"][0]["pulled_at"]  # the as-of stamp travels


def test_an_absent_dimension_is_null_never_zero(
    db_engine, db_session, tmp_path, crm_on
):
    """Delphi speaks no covers: event_covers is PRESENT and null in the
    payload — the frontend renders an honest gap, and a silent 0 cannot
    even be transmitted."""
    _seed(db_session)
    _store(db_session, [
        CrmDemandDay(stay_date=date(2026, 8, 6), rooms_on_books=132,
                     group_rooms=0, event_covers=None),
    ])
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier,
                InMemoryCrmFeed(feed_capabilities=_DELPHI_CAPS))
    (day,) = c.get("/api/crm/demand", headers=_gm(mint),
                   params=_WINDOW).json()["days"]
    assert "event_covers" in day and day["event_covers"] is None
    assert day["group_rooms"] == 0  # a spoken zero stays a zero


def test_roles_and_confinement(db_engine, db_session, tmp_path, crm_on):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier,
                InMemoryCrmFeed(feed_capabilities=_DELPHI_CAPS))

    assert c.get("/api/crm/demand", params=_WINDOW).status_code == 401
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='a')}"}
    assert c.get("/api/crm/demand", headers=acct,
                 params=_WINDOW).status_code == 403
    foreign = c.get("/api/crm/demand",
                    headers=_gm(mint, prop="SSSJ", sub="gm2"),
                    params=_WINDOW)
    assert foreign.status_code == 403
    assert "HISJ" not in foreign.json()["detail"]
    # A GM probing an unknown property learns nothing an out-of-scope one
    # would not; an org admin learns 404.
    assert c.get("/api/crm/demand", headers=_gm(mint),
                 params={**_WINDOW, "property": "NOPE"}).status_code == 403
    assert c.get("/api/crm/demand", headers=_admin(mint),
                 params={**_WINDOW, "property": "NOPE"}).status_code == 404
    assert c.get("/api/crm/demand", headers=_gm(mint),
                 params=_WINDOW).status_code == 200


def test_feature_off_degrades_to_not_configured(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The READ surface degrades (an honest gap, the J2 rollback story);
    only the PULL refuses loudly. Stored history is NOT served while the
    feature is off — a surface with no configured provider has no
    capability story to label it with."""
    monkeypatch.delenv("USALI_CRM_PROVIDER", raising=False)
    _seed(db_session)
    _store(db_session, [
        CrmDemandDay(stay_date=date(2026, 8, 6), rooms_on_books=132,
                     group_rooms=None, event_covers=None),
    ])
    db_session.commit()

    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    c = TestClient(app)
    r = c.get("/api/crm/demand", headers=_gm(mint), params=_WINDOW)
    assert r.status_code == 200
    assert r.json() == {
        "configured": False, "provider": None,
        "capabilities": {"rooms_on_books": False, "group_rooms": False,
                         "event_covers": False},
        "days": [],
    }


def test_window_refusals(db_engine, db_session, tmp_path, crm_on):
    """Inverted and unbounded windows are data errors, not empty
    successes."""
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier,
                InMemoryCrmFeed(feed_capabilities=_DELPHI_CAPS))

    inverted = c.get("/api/crm/demand", headers=_gm(mint), params={
        "property": "HISJ", "start": "2026-08-31", "end": "2026-08-01",
    })
    assert inverted.status_code == 422
    too_wide = c.get("/api/crm/demand", headers=_gm(mint), params={
        "property": "HISJ", "start": "2026-01-01", "end": "2027-06-01",
    })
    assert too_wide.status_code == 422

    # The boundary exactly: 366 days is the widest legal window, 367
    # refuses (the J7 review's 366→500 mutant survived a 517-day probe).
    start = date(2026, 1, 1)
    exactly = c.get("/api/crm/demand", headers=_gm(mint), params={
        "property": "HISJ", "start": start.isoformat(),
        "end": (start + timedelta(days=366)).isoformat(),
    })
    assert exactly.status_code == 200
    over = c.get("/api/crm/demand", headers=_gm(mint), params={
        "property": "HISJ", "start": start.isoformat(),
        "end": (start + timedelta(days=367)).isoformat(),
    })
    assert over.status_code == 422


def test_the_read_is_not_an_audit_writer(
    db_engine, db_session, tmp_path, crm_on
):
    """Demand is decision-support working data on a manager surface (the
    forecast-GET class), not PII egress — reads leave no audit rows, and
    pinning that keeps the audit trail meaningful."""
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier,
                InMemoryCrmFeed(feed_capabilities=_DELPHI_CAPS))
    assert c.get("/api/crm/demand", headers=_gm(mint),
                 params=_WINDOW).status_code == 200
    assert db_session.execute(select(AuditEvent)).scalars().all() == []
