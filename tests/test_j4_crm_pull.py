"""J4: the audited pull + the demand read helpers (plan decision 6).

A pull is an EXPLICIT, audited act: POST /api/crm/refresh resolves the
property's crm_ref from the registry, calls the configured feed over the
bounded horizon (property-local today .. +90d), writes ONE batch of
append-only snapshots, audits pointing at the batch, and returns counts
plus the dropped-field report. No background scheduler — cadence is a
deployment concern.

Readers: `latest_demand` takes the NEWEST batch per stay-date (current
demand); `demand_pace` pairs it with the previous batch's row (pace is a
comparison of snapshots — the reason the table is append-only).

Feature-off is PINNED here as a loud 503 naming the config switch (the
plan offered 404-the-router or refuse-loudly; a named refusal is the G1
absence posture, and it cannot be confused with a typo'd path).
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.config import get_settings
from usali.crm_feed import CrmCapabilities, CrmDemandDay, CrmFeedError, InMemoryCrmFeed
from usali.crm_pull import demand_pace, latest_demand, store_pull
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    CrmDemandSnapshot,
    CrmPullBatch,
    Organization,
    OrgSettings,
    Property,
)
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _seed(db_session):
    """HISJ carries a crm_ref; SSSJ deliberately has none (the refusal
    case). Same two-property shape as the schedule API tests."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.flush()  # org row before its org_settings FK child
    # L5: the provider is per-org now — org 1's org_settings row carries it,
    # seeded from the env default exactly as ensure_default_org does (the
    # crm_on fixture sets USALI_CRM_PROVIDER=delphi; feature-off tests leave
    # it empty). At runtime the crm router reads THIS row, not env.
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
    grant_role(db_session, "accountant", sub="acc")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")


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


def _days():
    d1, d2 = _horizon_days()
    return [
        CrmDemandDay(stay_date=d1, rooms_on_books=132,
                     group_rooms=50, event_covers=None,
                     labels=("Acme Corp Annual", "Delta Sigma Reunion")),
        CrmDemandDay(stay_date=d2, rooms_on_books=118,
                     group_rooms=30, event_covers=None,
                     labels=("Acme Corp Annual",)),
    ]


@pytest.fixture
def crm_on(monkeypatch):
    monkeypatch.setenv("USALI_CRM_PROVIDER", "delphi")


def _horizon_days():
    """The two consecutive in-horizon stay dates the endpoint tests feed and
    assert on. Anchored to the property-local today the pull uses (HISJ
    defaults to America/Los_Angeles) and offset a couple days in, so they stay
    inside the [today, today+90] horizon as real time advances — a hardcoded
    calendar date rots out of the window and the pull refuses it (502) before
    the behavior under test runs."""
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    return today + timedelta(days=1), today + timedelta(days=2)


# --- the pull endpoint -------------------------------------------------------


def test_refresh_writes_a_batch_and_audits(
    db_engine, db_session, tmp_path, crm_on
):
    """One pull = one batch + its snapshot rows + one AuditEvent pointing
    at the batch (the settlement idiom). The horizon is bounded (90 days
    from property-local today) and is exactly what the feed was asked."""
    _seed(db_session)
    verifier, mint = make_authkit()
    feed = InMemoryCrmFeed(days=_days(), dropped_fields={"Contact": 3})
    c = _client(db_engine, tmp_path, verifier, feed)

    from datetime import datetime
    from zoneinfo import ZoneInfo

    before = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    r = c.post("/api/crm/refresh", headers=_gm(mint),
               json={"property": "HISJ"})
    after = datetime.now(ZoneInfo("America/Los_Angeles")).date()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["property_id"] == "HISJ"
    assert body["provider"] == "delphi"
    assert body["days_written"] == 2
    assert body["dropped_fields"] == {"Contact": 3}

    batch = db_session.execute(select(CrmPullBatch)).scalar_one()
    assert batch.batch_id == body["batch_id"]
    assert batch.property_id == "HISJ"
    assert batch.provider == "delphi"
    assert (batch.horizon_end - batch.horizon_start).days == 90
    # Property-local "today", bracketed by the property's own clock read
    # before and after the call: the ±1-day tolerance this replaced
    # absorbed a real off-by-one at the horizon anchor (J7 review).
    assert before <= batch.horizon_start <= after
    assert feed.calls == [("REF-1", batch.horizon_start, batch.horizon_end)]

    rows = db_session.execute(
        select(CrmDemandSnapshot).order_by(CrmDemandSnapshot.stay_date)
    ).scalars().all()
    d1, d2 = _horizon_days()
    assert [(s.stay_date, s.rooms_on_books, s.group_rooms, s.event_covers)
            for s in rows] == [
        (d1, 132, 50, None),
        (d2, 118, 30, None),
    ]
    assert rows[0].labels == "Acme Corp Annual, Delta Sigma Reunion"

    audit = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "crm_refresh")
    ).scalar_one()
    assert audit.actor_subject == "gm"
    assert audit.resource_type == "crm_pull_batch"
    assert audit.resource_id == str(batch.batch_id)


def test_a_re_pull_appends_never_updates(
    db_engine, db_session, tmp_path, crm_on
):
    """The append-only design working end to end: a second refresh writes
    a NEW batch carrying the same stay dates; nothing is overwritten, and
    the latest reader takes the newer voice."""
    _seed(db_session)
    verifier, mint = make_authkit()
    feed = InMemoryCrmFeed(days=_days())
    c = _client(db_engine, tmp_path, verifier, feed)

    d1, d2 = _horizon_days()
    assert c.post("/api/crm/refresh", headers=_gm(mint),
                  json={"property": "HISJ"}).status_code == 201
    feed.days = [
        CrmDemandDay(stay_date=d1, rooms_on_books=140,
                     group_rooms=55, event_covers=None,
                     labels=("Acme Corp Annual", "Delta Sigma Reunion")),
    ]
    assert c.post("/api/crm/refresh", headers=_gm(mint),
                  json={"property": "HISJ"}).status_code == 201

    batches = db_session.execute(select(CrmPullBatch)).scalars().all()
    assert len(batches) == 2
    aug6 = db_session.execute(
        select(CrmDemandSnapshot)
        .where(CrmDemandSnapshot.stay_date == d1)
        .order_by(CrmDemandSnapshot.batch_id)
    ).scalars().all()
    assert [s.rooms_on_books for s in aug6] == [132, 140]  # both voices kept

    current = latest_demand(
        db_session, "HISJ", d1, d2
    )
    by_date = {d.stay_date: d for d in current}
    assert by_date[d1].rooms_on_books == 140  # newest batch
    # Aug 7 is GONE, not stale: the second pull covered the full horizon
    # and stated nothing for it — silence within a covered horizon is a
    # cancellation, and serving the old 118 would staff a dead event
    # (the J7 money High).
    assert d2 not in by_date


def test_roles_and_property_confinement(
    db_engine, db_session, tmp_path, crm_on
):
    """Scheduler roles only (org_admin / property_gm), property-confined
    via assignment scope — the schedule_api convention. The 403 detail
    names nothing."""
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryCrmFeed(days=_days()))

    assert c.post("/api/crm/refresh",
                  json={"property": "HISJ"}).status_code == 401
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acc')}"}
    assert c.post("/api/crm/refresh", headers=acct,
                  json={"property": "HISJ"}).status_code == 403

    foreign = c.post("/api/crm/refresh",
                     headers=_gm(mint, prop="SSSJ", sub="gm2"),
                     json={"property": "HISJ"})
    assert foreign.status_code == 403
    assert "HISJ" not in foreign.json()["detail"]
    assert db_session.execute(select(CrmPullBatch)).scalars().all() == []

    assert c.post("/api/crm/refresh", headers=_admin(mint),
                  json={"property": "HISJ"}).status_code == 201


def test_an_unknown_property_is_404_for_admin_403_for_gm(
    db_engine, db_session, tmp_path, crm_on
):
    """Scope check FIRST: a GM probing an unknown property gets the same
    403 as any out-of-scope property (no existence oracle); an org admin,
    who may act anywhere, learns 404."""
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryCrmFeed(days=_days()))

    assert c.post("/api/crm/refresh", headers=_gm(mint),
                  json={"property": "NOPE"}).status_code == 403
    assert c.post("/api/crm/refresh", headers=_admin(mint),
                  json={"property": "NOPE"}).status_code == 404


def test_a_property_without_crm_ref_refuses_by_name(
    db_engine, db_session, tmp_path, crm_on
):
    """NULL crm_ref is the honest 'no CRM speaks for this property' — the
    refusal names the property (the wage-jurisdiction posture: silence is
    answered with a named refusal, never a guess), and the probe is
    audited (the I6 lesson)."""
    _seed(db_session)
    verifier, mint = make_authkit()
    feed = InMemoryCrmFeed(days=_days())
    c = _client(db_engine, tmp_path, verifier, feed)

    r = c.post("/api/crm/refresh", headers=_admin(mint),
               json={"property": "SSSJ"})
    assert r.status_code == 409
    assert "SSSJ" in r.json()["detail"]
    assert "crm_ref" in r.json()["detail"]
    assert feed.calls == []  # refused before any provider traffic

    refused = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "crm_refresh_refused")
    ).scalar_one()
    assert refused.resource_type == "property"
    assert refused.resource_id == "SSSJ"


def test_feature_off_is_a_loud_503_naming_the_switch(
    db_engine, db_session, tmp_path, monkeypatch
):
    """THE feature-off pin (the plan offered two shapes; this is the one):
    the router stays mounted and refuses loudly, naming the config switch
    — an unconfigured feed can never read as a typo'd path or a silent
    no-op. Nothing is written, and the refusal is audited."""
    monkeypatch.delenv("USALI_CRM_PROVIDER", raising=False)
    _seed(db_session)
    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    c = TestClient(app)

    r = c.post("/api/crm/refresh", headers=_admin(mint),
               json={"property": "HISJ"})
    assert r.status_code == 503
    assert "USALI_CRM_PROVIDER" in r.json()["detail"]
    assert db_session.execute(select(CrmPullBatch)).scalars().all() == []
    refused = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "crm_refresh_refused")
    ).scalar_one()
    assert refused.resource_id == "HISJ"


def test_an_unknown_provider_name_fails_app_construction(
    db_engine, tmp_path, monkeypatch
):
    monkeypatch.setenv("USALI_CRM_PROVIDER", "hubspot")
    with pytest.raises(RuntimeError, match="hubspot"):
        create_app(
            inbox_dir=tmp_path / "inbox",
            processed_dir=tmp_path / "processed",
            failed_dir=tmp_path / "failed",
            session_factory=make_session_factory(db_engine),
        )


def test_a_provider_failure_is_502_audited_and_writes_nothing(
    db_engine, db_session, tmp_path, crm_on
):
    """A failed pull writes NO batch and no snapshots (a half-written pull
    would read as a real as-of), audits the failure, and surfaces the
    adapter's message — which is body-free by construction (J3)."""
    _seed(db_session)
    verifier, mint = make_authkit()

    class FailingFeed:
        def capabilities(self):
            return CrmCapabilities()

        def fetch_demand(self, external_ref, start, end):
            raise CrmFeedError("delphi request failed (500)")

    c = _client(db_engine, tmp_path, verifier, FailingFeed())
    r = c.post("/api/crm/refresh", headers=_gm(mint),
               json={"property": "HISJ"})
    assert r.status_code == 502
    assert "delphi request failed (500)" in r.json()["detail"]
    assert db_session.execute(select(CrmPullBatch)).scalars().all() == []
    assert db_session.execute(select(CrmDemandSnapshot)).scalars().all() == []
    refused = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "crm_refresh_refused")
    ).scalar_one()
    assert refused.resource_id == "HISJ"


def test_stored_labels_are_joined_and_bounded(
    db_engine, db_session, tmp_path, crm_on
):
    """The snapshot's label string is comma-joined display text bounded to
    the column (300) — a CRM with forty long block names cannot overflow
    the write."""
    _seed(db_session)
    verifier, mint = make_authkit()
    many = tuple(f"Block {i} {'x' * 40}" for i in range(10))
    d1, _ = _horizon_days()
    feed = InMemoryCrmFeed(days=[
        CrmDemandDay(stay_date=d1, rooms_on_books=None,
                     group_rooms=10, event_covers=None, labels=many),
    ])
    c = _client(db_engine, tmp_path, verifier, feed)

    assert c.post("/api/crm/refresh", headers=_gm(mint),
                  json={"property": "HISJ"}).status_code == 201
    row = db_session.execute(select(CrmDemandSnapshot)).scalar_one()
    assert row.labels.startswith("Block 0")
    # Exactly the column bound: an overflowing join truncates AT 300 —
    # any tighter would silently lose scheduler display text (the J7
    # review's 300→200 mutant survived a `<= 300` assertion).
    assert len(row.labels) == 300


# --- the read helpers --------------------------------------------------------


def _batch(session, property_id, days, *, minutes_later=0, horizon=None):
    """Write a batch directly with a controlled pulled_at ordering.
    `horizon` defaults to exactly the days written — pass a wider one to
    model a pull that COVERED a day and stated nothing about it."""
    from datetime import UTC, datetime

    from usali.crm_feed import CrmDemandPull

    start, end = horizon or (
        min(d.stay_date for d in days), max(d.stay_date for d in days)
    )
    batch = store_pull(
        session, property_id=property_id, provider="delphi",
        horizon_start=start, horizon_end=end,
        pull=CrmDemandPull(days=tuple(days)),
    )
    batch.pulled_at = (
        datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
        + timedelta(minutes=minutes_later)
    )
    session.flush()
    return batch


def _day(stay, rooms):
    return CrmDemandDay(stay_date=stay, rooms_on_books=rooms,
                        group_rooms=None, event_covers=None)


def test_latest_demand_takes_the_newest_batch_per_stay_date(db_session):
    """Latest is PER STAY DATE, not per batch: a newer batch that covers
    only part of the window wins where it speaks, while older batches
    keep speaking for the days it does not."""
    _seed(db_session)
    _batch(db_session, "HISJ",
           [_day(date(2026, 8, 6), 132), _day(date(2026, 8, 7), 118)])
    _batch(db_session, "HISJ",
           [_day(date(2026, 8, 7), 125)], minutes_later=60)
    db_session.commit()

    rows = latest_demand(
        db_session, "HISJ", date(2026, 8, 1), date(2026, 8, 31)
    )
    assert [(r.stay_date, r.rooms_on_books) for r in rows] == [
        (date(2026, 8, 6), 132),
        (date(2026, 8, 7), 125),
    ]
    # Windowed and property-confined.
    assert latest_demand(
        db_session, "HISJ", date(2026, 8, 8), date(2026, 8, 31)
    ) == []
    assert latest_demand(
        db_session, "SSSJ", date(2026, 8, 1), date(2026, 8, 31)
    ) == []


def test_j7_a_cancelled_day_stops_being_current(db_session):
    """The J7 money High: a newer pull that COVERED a stay-date and said
    nothing about it means the demand is GONE (the block cancelled, the
    event dropped off the books) — not that last week's figure is still
    current. Without horizon awareness a cancelled fat Thursday is
    immortal: every re-pull re-reads the stale 200 rooms as today's
    demand until the horizon rolls past the date."""
    _seed(db_session)
    horizon = (date(2026, 8, 1), date(2026, 8, 31))
    _batch(db_session, "HISJ",
           [_day(date(2026, 8, 6), 200), _day(date(2026, 8, 7), 118)],
           horizon=horizon)
    _batch(db_session, "HISJ", [_day(date(2026, 8, 7), 125)],
           minutes_later=60, horizon=horizon)
    db_session.commit()

    rows = latest_demand(
        db_session, "HISJ", date(2026, 8, 1), date(2026, 8, 31)
    )
    # Aug 6 is GONE — the newer pull covered it and stated no demand.
    assert [(r.stay_date, r.rooms_on_books) for r in rows] == [
        (date(2026, 8, 7), 125),
    ]

    # Pace still sees the cancellation's history for Aug 7; a day whose
    # current voice is silence has no current to pace against.
    pace = demand_pace(
        db_session, "HISJ", date(2026, 8, 1), date(2026, 8, 31)
    )
    assert [p.stay_date for p in pace] == [date(2026, 8, 7)]


def test_j7_newest_voice_is_pulled_at_not_insert_order(db_session):
    """`pulled_at` is the honest as-of stamp; batch_id only breaks ties.
    A backfilled batch inserted LATER (higher id) carrying an OLDER
    pulled_at must not outrank the genuinely newer pull."""
    _seed(db_session)
    _batch(db_session, "HISJ", [_day(date(2026, 8, 6), 140)],
           minutes_later=60)  # newer pull, lower batch_id
    _batch(db_session, "HISJ", [_day(date(2026, 8, 6), 90)],
           minutes_later=0)   # older pull, higher batch_id (backfill)
    db_session.commit()

    (row,) = latest_demand(
        db_session, "HISJ", date(2026, 8, 1), date(2026, 8, 31)
    )
    assert row.rooms_on_books == 140


def test_a_same_instant_tie_breaks_on_batch_id(db_session):
    """Two pulls stamped the same instant (one clock tick, or a backfill)
    must still resolve deterministically: the higher batch_id — the later
    insert — wins. Without the tiebreak 'current demand' would flap.

    Batch ids are set EXPLICITLY, with the higher id inserted first, so
    the assertion cannot pass on incidental engine row order (the J7
    review caught the previous shape doing exactly that)."""
    from datetime import UTC, datetime

    from usali.models import CrmDemandSnapshot as Snap

    _seed(db_session)
    stamp = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    for batch_id, rooms in ((100, 100), (500, 111)):
        db_session.add(CrmPullBatch(
            batch_id=batch_id, property_id="HISJ", provider="delphi",
            horizon_start=date(2026, 8, 1), horizon_end=date(2026, 8, 31),
            pulled_at=stamp,
        ))
        db_session.flush()
        db_session.add(Snap(batch_id=batch_id, stay_date=date(2026, 8, 6),
                            rooms_on_books=rooms, labels=""))
    db_session.commit()

    (row,) = latest_demand(
        db_session, "HISJ", date(2026, 8, 1), date(2026, 8, 31)
    )
    assert row.rooms_on_books == 111
    assert row.batch_id == 500


def test_demand_pace_pairs_latest_with_previous(db_session):
    """Pace is the whole reason the table is append-only: each stay-date
    pairs its newest voice with the previous batch's, so '140 today vs 90
    last pull' is computable. A single-voice day has no previous."""
    _seed(db_session)
    _batch(db_session, "HISJ", [_day(date(2026, 8, 6), 90)])
    _batch(db_session, "HISJ", [_day(date(2026, 8, 6), 120)],
           minutes_later=30)
    _batch(db_session, "HISJ",
           [_day(date(2026, 8, 6), 140), _day(date(2026, 8, 7), 60)],
           minutes_later=60)
    db_session.commit()

    pace = demand_pace(
        db_session, "HISJ", date(2026, 8, 1), date(2026, 8, 31)
    )
    by_date = {p.stay_date: p for p in pace}
    aug6 = by_date[date(2026, 8, 6)]
    assert aug6.current.rooms_on_books == 140
    assert aug6.previous is not None
    assert aug6.previous.rooms_on_books == 120  # the 2nd batch, not the 1st
    aug7 = by_date[date(2026, 8, 7)]
    assert aug7.current.rooms_on_books == 60
    assert aug7.previous is None


# --- the J7 review pins ------------------------------------------------------


def test_j7_a_malformed_pull_is_502_audited_and_writes_nothing(
    db_engine, db_session, tmp_path, crm_on
):
    """Two provider-misbehavior shapes the J7 review found unhandled: a
    duplicate stay-date (was a raw IntegrityError 500) and a day outside
    the pulled horizon (was silently STORED under a batch whose horizon
    said otherwise). Both are the same class as a failed fetch: refuse
    502, audit, write nothing."""
    _seed(db_session)
    verifier, mint = make_authkit()

    d1, _ = _horizon_days()  # in-horizon, so the DUPLICATE is what's refused
    dup = InMemoryCrmFeed(days=[
        _day(d1, 100), _day(d1, 111),
    ])
    c = _client(db_engine, tmp_path, verifier, dup)
    r = c.post("/api/crm/refresh", headers=_gm(mint),
               json={"property": "HISJ"})
    assert r.status_code == 502
    assert "duplicate" in r.json()["detail"]
    assert db_session.execute(select(CrmPullBatch)).scalars().all() == []
    assert db_session.execute(select(CrmDemandSnapshot)).scalars().all() == []

    rogue = InMemoryCrmFeed(days=[_day(date(2031, 1, 1), 100)])
    c = _client(db_engine, tmp_path, verifier, rogue)
    r = c.post("/api/crm/refresh", headers=_gm(mint),
               json={"property": "HISJ"})
    assert r.status_code == 502
    assert "horizon" in r.json()["detail"]
    assert db_session.execute(select(CrmPullBatch)).scalars().all() == []

    refusals = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "crm_refresh_refused")
    ).scalars().all()
    assert len(refusals) == 2


def test_j7_provider_named_but_factory_empty_still_refuses(
    db_engine, db_session, tmp_path, crm_on
):
    """The feature-off predicate is an OR of two honest absences: the
    divergent state (provider named, factory yields None) must refuse
    the pull and degrade the read exactly like fully-off — the J7
    review's `or`→`and` mutant survived because only the aligned states
    were ever tested."""
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, None)  # factory yields None

    r = c.post("/api/crm/refresh", headers=_gm(mint),
               json={"property": "HISJ"})
    assert r.status_code == 503

    r = c.get("/api/crm/demand", headers=_gm(mint), params={
        "property": "HISJ", "start": "2026-08-01", "end": "2026-08-31",
    })
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_j7_reader_window_edges_are_exact(db_session):
    """Both stay-date edges and both horizon-intersection edges: a batch
    whose horizon ENDS at the window start (and one that BEGINS at the
    window end) still speaks — the J7 review found the edge pins lived
    only in the demo-seed fixture by accident."""
    _seed(db_session)
    _batch(db_session, "HISJ",
           [_day(date(2026, 8, 5), 90), _day(date(2026, 8, 6), 100)],
           horizon=(date(2026, 8, 1), date(2026, 8, 6)))
    _batch(db_session, "HISJ",
           [_day(date(2026, 8, 7), 110), _day(date(2026, 8, 8), 120)],
           horizon=(date(2026, 8, 7), date(2026, 8, 9)))
    db_session.commit()

    rows = latest_demand(
        db_session, "HISJ", date(2026, 8, 6), date(2026, 8, 7)
    )
    assert [(r.stay_date, r.rooms_on_books) for r in rows] == [
        (date(2026, 8, 6), 100),
        (date(2026, 8, 7), 110),
    ]
