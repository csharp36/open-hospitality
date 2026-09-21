"""OH-23 Task 4: the property's night-audit email address and its event log.

Five operator endpoints under `/api/properties/{property_id}` (D-OH23.8): read
the address, create it, rotate it, set the sender allowlist, and list the
recent intake events. The cases here hold three things the design asks for and
one the schema asks for:

* the gates are the property-config gates — reads like `get_config`, writes
  like `set_fiscal_calendar` — so a GM of another property is refused on every
  verb (`test_every_verb_confines_a_gm_of_another_property`);
* `property_intake_address` carries NO RLS policy (D-OH23.3), so the org
  filter is the route's own; `test_an_address_of_another_org_is_invisible_and_unrotatable`
  is the pin `usali/models.py` and `o1a0intake` both name;
* one live address per property is the DATABASE's rule
  (`uq_property_intake_address_active`), so a second create is a 409 out of an
  IntegrityError rather than a read-then-write race;
* a rotated address still resolves, and a message to it is recorded
  `revoked_address` — the operator's evidence the PMS has not been updated.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from tests.intake_helpers import _message, _post
from tests.orgworld import ORG1_ADMIN, ORG2_ALIAS, rls_client
from usali.auth import ACTIVE_ORG_HEADER
from usali.config import get_settings
from usali.db import make_session_factory
from usali.intake_address_api import _active_address
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali import intake, intake_address_api
from usali.models import (
    AuditEvent,
    EmailIntakeEvent,
    IngestBatch,
    Organization,
    Property,
    PropertyIntakeAddress,
)
from usali.server import create_app
from usali.tenancy import bind_org_context

_DOMAIN = get_settings().email_intake_domain


# --- the world ---------------------------------------------------------------


def _client(db_engine, tmp_path, verifier) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app)


def _org_and_property(db_session, pid="HISJ"):
    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    db_session.commit()


def _admin_headers(mint, db_session):
    grant_role(db_session, "org_admin", sub="intake-admin", org_id=1)
    tok = mint(roles=["org_admin"], sub="intake-admin")
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def world(db_engine, db_session, tmp_path):
    """HISJ under org 1, an org_admin who may write it, and a client."""
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    return client, _admin_headers(mint, db_session)


def _addresses(db_session, property_id="HISJ"):
    return list(db_session.scalars(
        select(PropertyIntakeAddress)
        .where(PropertyIntakeAddress.property_id == property_id)
        .order_by(PropertyIntakeAddress.address_id)
    ))


def _audits(db_session, action):
    return list(db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == action)
        .order_by(AuditEvent.event_id)
    ))


def _event(db_session, *, property_id="HISJ", address_id=1, received_at,
           outcome="ingested", envelope_from="nightaudit@pms.test",
           subject="Night audit", auth_result=None, message_id=None,
           attachments=()):
    row = EmailIntakeEvent(
        address_id=address_id, property_id=property_id, received_at=received_at,
        envelope_from=envelope_from, subject=subject, auth_result=auth_result,
        message_id=message_id, outcome=outcome, attachments=list(attachments),
    )
    db_session.add(row)
    db_session.commit()
    return row


# --- GET / POST: the address ---------------------------------------------------


def test_get_answers_null_before_an_address_exists(world):
    client, headers = world
    r = client.get("/api/properties/HISJ/intake-address", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"address": None, "local_part": None,
                        "created_at": None, "sender_domains": None}


def test_create_mints_one_address_and_audits_it(world, db_session):
    client, headers = world
    r = client.post("/api/properties/HISJ/intake-address", headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["local_part"].startswith("na-")
    # The domain is served by the API, never hard-coded in the SPA (D-OH23.8).
    assert body["address"] == f"{body['local_part']}@{_DOMAIN}"
    assert body["sender_domains"] is None  # any authenticated sender (D-OH23.4)
    assert body["created_at"] is not None

    rows = _addresses(db_session)
    assert [(row.local_part, row.revoked_at) for row in rows] == [(body["local_part"], None)]
    assert rows[0].org_id == 1

    audits = _audits(db_session, "intake_address_created")
    assert len(audits) == 1
    assert audits[0].resource_type == "property_intake_address"
    assert audits[0].resource_id == body["local_part"]
    assert audits[0].actor_subject == "intake-admin"

    again = client.get("/api/properties/HISJ/intake-address", headers=headers)
    assert again.json()["address"] == body["address"]


def test_a_second_create_is_refused_and_leaves_one_active_address(world, db_session):
    client, headers = world
    first = client.post("/api/properties/HISJ/intake-address", headers=headers)
    assert first.status_code == 201, first.text

    second = client.post("/api/properties/HISJ/intake-address", headers=headers)
    assert second.status_code == 409, second.text
    assert "active" in second.json()["detail"]

    rows = _addresses(db_session)
    assert [row.local_part for row in rows] == [first.json()["local_part"]]
    assert len(_audits(db_session, "intake_address_created")) == 1


def test_a_gm_of_the_property_may_create_and_read_it(db_engine, db_session, tmp_path):
    """The write gate is `require_grants(ORG_ADMIN, PROPERTY_GM)`, as
    `set_fiscal_calendar`'s is — a GM of THIS property is not an admin and
    must still be able to mint the address."""
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "property_gm", sub="gm-hisj", property_id="HISJ")
    tok = mint(roles=["property_gm"], sub="gm-hisj",
               scopes=[{"property_id": "HISJ", "department_id": None}])
    headers = {"Authorization": f"Bearer {tok}"}

    made = client.post("/api/properties/HISJ/intake-address", headers=headers)
    assert made.status_code == 201, made.text
    read = client.get("/api/properties/HISJ/intake-address", headers=headers)
    assert read.json()["address"] == made.json()["address"]


# --- rotate ---------------------------------------------------------------------


def test_rotate_revokes_the_old_row_and_mints_a_new_one(world, db_session):
    client, headers = world
    old = client.post("/api/properties/HISJ/intake-address", headers=headers).json()

    r = client.post("/api/properties/HISJ/intake-address/rotate", headers=headers)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["local_part"] != old["local_part"]
    assert new["sender_domains"] is None

    rows = _addresses(db_session)
    assert len(rows) == 2
    revoked, live = rows
    assert revoked.local_part == old["local_part"] and revoked.revoked_at is not None
    assert live.local_part == new["local_part"] and live.revoked_at is None

    assert client.get("/api/properties/HISJ/intake-address",
                      headers=headers).json()["address"] == new["address"]
    # Both halves of the rotation are in the trail: which capability was
    # retired, and which one replaced it.
    assert [a.resource_id for a in _audits(db_session, "intake_address_revoked")] == [
        old["local_part"]]
    assert [a.resource_id for a in _audits(db_session, "intake_address_rotated")] == [
        new["local_part"]]


def test_rotate_carries_the_allowlist_nowhere_and_starts_open(world, db_session):
    """A rotation mints a NEW capability; the old row's allowlist is not
    copied onto it, so the operator sets the policy again deliberately."""
    client, headers = world
    client.post("/api/properties/HISJ/intake-address", headers=headers)
    client.put("/api/properties/HISJ/intake-address",
               json={"sender_domains": ["pms.test"]}, headers=headers)

    rotated = client.post("/api/properties/HISJ/intake-address/rotate", headers=headers)
    assert rotated.json()["sender_domains"] is None
    revoked, live = _addresses(db_session)
    assert revoked.sender_domains == ["pms.test"]
    assert live.sender_domains is None


def test_rotate_without_an_address_is_404(world, db_session):
    client, headers = world
    r = client.post("/api/properties/HISJ/intake-address/rotate", headers=headers)
    # 404 and not an implicit create: with no address the page offers
    # "Create address", so a rotate here is a stale client (D-OH23.8). The
    # detail is asserted because a missing ROUTE is also a 404: without it
    # this case passes with the router unmounted.
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "this property has no active intake address"
    assert _addresses(db_session) == []


def test_mail_to_a_rotated_address_is_recorded_as_revoked_address(world, db_session):
    """The rotation is worth nothing unless the old capability stops working,
    and the operator needs to SEE the PMS still using it (D-OH23.7)."""
    client, headers = world
    old = client.post("/api/properties/HISJ/intake-address", headers=headers).json()
    new = client.post("/api/properties/HISJ/intake-address/rotate",
                      headers=headers).json()

    stale = _post(client, _message(), to=old["address"])
    assert stale.status_code == 200, stale.text
    assert stale.json()["outcome"] == "revoked_address"

    fresh = _post(client, _message(), to=new["address"])
    assert fresh.json()["outcome"] == "no_attachment"  # live, just empty

    listed = client.get("/api/properties/HISJ/intake-events", headers=headers).json()
    assert [e["outcome"] for e in listed["events"]] == ["no_attachment", "revoked_address"]


# --- PUT: the sender allowlist (D-OH23.4) ---------------------------------------


def test_the_allowlist_refuses_mail_from_an_excluded_domain(world, db_session):
    """The allowlist is only worth setting if it reaches the webhook. Both
    messages carry an ALIGNED dkim pass for their own domain, so the only
    thing separating them is `sender_domains` (D-OH23.4)."""
    client, headers = world
    address = client.post("/api/properties/HISJ/intake-address",
                          headers=headers).json()["address"]
    client.put("/api/properties/HISJ/intake-address",
               json={"sender_domains": ["pms.test"]}, headers=headers)

    outsider = _post(client, _message(), to=address, frm="nightaudit@other.test")
    assert outsider.status_code == 200, outsider.text
    assert outsider.json()["outcome"] == "sender_rejected"

    listed = client.get("/api/properties/HISJ/intake-events", headers=headers).json()
    assert [e["outcome"] for e in listed["events"]] == ["sender_rejected"]
    assert [e["envelope_from"] for e in listed["events"]] == ["nightaudit@other.test"]
    # Refused at the door: nothing of the message was opened or staged.
    assert db_session.scalars(select(IngestBatch)).all() == []

    listed_sender = _post(client, _message(), to=address, frm="nightaudit@pms.test")
    assert listed_sender.json()["outcome"] != "sender_rejected"




def test_the_sender_allowlist_round_trips_lowercased_and_deduplicated(world, db_session):
    client, headers = world
    client.post("/api/properties/HISJ/intake-address", headers=headers)

    r = client.put("/api/properties/HISJ/intake-address",
                   json={"sender_domains": ["PMS.test", "pms.test", "mail.hotel.example"]},
                   headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["sender_domains"] == ["pms.test", "mail.hotel.example"]

    read = client.get("/api/properties/HISJ/intake-address", headers=headers)
    assert read.json()["sender_domains"] == ["pms.test", "mail.hotel.example"]
    assert _addresses(db_session)[0].sender_domains == ["pms.test", "mail.hotel.example"]

    audits = _audits(db_session, "intake_sender_domains_set")
    assert [a.resource_id for a in audits] == [read.json()["local_part"]]


def test_an_empty_allowlist_stores_null(world, db_session):
    """NULL is "any authenticated sender" (D-OH23.4); an empty JSON array
    would be a different, unreachable policy."""
    client, headers = world
    client.post("/api/properties/HISJ/intake-address", headers=headers)
    client.put("/api/properties/HISJ/intake-address",
               json={"sender_domains": ["pms.test"]}, headers=headers)

    r = client.put("/api/properties/HISJ/intake-address",
                   json={"sender_domains": []}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["sender_domains"] is None
    assert _addresses(db_session)[0].sender_domains is None


@pytest.mark.parametrize("entry", [
    "not a domain", "pms", "-pms.test", "pms.test-", "pms..test",
    "pms.test/path", "*.pms.test", "nightaudit@pms.test", "pms .test",
    "a" * 64 + ".test",   # RFC 1035 caps a LABEL at 63, not just the name
])
def test_an_invalid_sender_domain_is_refused_naming_the_entry(world, db_session, entry):
    client, headers = world
    client.post("/api/properties/HISJ/intake-address", headers=headers)
    r = client.put("/api/properties/HISJ/intake-address",
                   json={"sender_domains": ["pms.test", entry]}, headers=headers)
    assert r.status_code == 422, r.text
    assert entry.strip().lower() in r.json()["detail"]
    assert _addresses(db_session)[0].sender_domains is None


def test_too_many_sender_domains_is_refused(world, db_session):
    client, headers = world
    client.post("/api/properties/HISJ/intake-address", headers=headers)
    many = [f"pms{n}.test" for n in range(21)]
    r = client.put("/api/properties/HISJ/intake-address",
                   json={"sender_domains": many}, headers=headers)
    assert r.status_code == 422, r.text
    assert _addresses(db_session)[0].sender_domains is None


def test_an_over_long_sender_domain_is_refused_without_echoing_it(world):
    client, headers = world
    client.post("/api/properties/HISJ/intake-address", headers=headers)
    huge = ("a" * 60 + ".") * 5 + "test"
    r = client.put("/api/properties/HISJ/intake-address",
                   json={"sender_domains": [huge]}, headers=headers)
    assert r.status_code == 422, r.text
    assert huge not in r.json()["detail"]


def test_the_allowlist_needs_an_active_address(world):
    client, headers = world
    r = client.put("/api/properties/HISJ/intake-address",
                   json={"sender_domains": ["pms.test"]}, headers=headers)
    assert r.status_code == 404, r.text
    # The route's own words, not the 404 an unmounted router would give.
    assert r.json()["detail"] == "this property has no active intake address"


# --- GET: the event log ---------------------------------------------------------


def test_events_are_newest_first_and_honor_the_limit(world, db_session):
    client, headers = world
    base = datetime(2026, 7, 7, 4, 0, tzinfo=UTC)
    for n, outcome in enumerate(["ingested", "sender_rejected", "duplicate"]):
        _event(db_session, received_at=base + timedelta(hours=n), outcome=outcome)
    # Another property's events never appear in this property's log.
    _org_and_property(db_session, "SSSJ")
    _event(db_session, property_id="SSSJ", received_at=base + timedelta(hours=9),
           outcome="failed")

    r = client.get("/api/properties/HISJ/intake-events", headers=headers)
    assert r.status_code == 200, r.text
    assert [e["outcome"] for e in r.json()["events"]] == [
        "duplicate", "sender_rejected", "ingested"]

    limited = client.get("/api/properties/HISJ/intake-events?limit=2", headers=headers)
    assert [e["outcome"] for e in limited.json()["events"]] == ["duplicate", "sender_rejected"]


def test_an_event_carries_its_attachments_and_not_the_auth_result(world, db_session):
    client, headers = world
    _event(db_session, received_at=datetime(2026, 7, 7, 4, 0, tzinfo=UTC),
           auth_result="dkim=pass header.d=pms.test", message_id="<one@pms.test>",
           attachments=[{"name": "flash.pdf", "sha256": "ab" * 32, "bytes": 12,
                         "outcome": "ingested", "batch_id": 7}])
    r = client.get("/api/properties/HISJ/intake-events", headers=headers)
    event = r.json()["events"][0]
    assert set(event) == {"event_id", "received_at", "envelope_from", "subject",
                          "outcome", "message_id", "attachments"}
    assert event["attachments"] == [{"name": "flash.pdf", "sha256": "ab" * 32,
                                     "bytes": 12, "outcome": "ingested", "batch_id": 7}]


def test_two_events_at_the_same_instant_list_by_event_id(world, db_session):
    """`received_at` alone does not order two messages recorded in the same
    transaction — the `event_id` tiebreak does, and this is the only case
    that can tell whether it is still there."""
    client, headers = world
    same = datetime(2026, 7, 7, 4, 0, tzinfo=UTC)
    first = _event(db_session, received_at=same, outcome="ingested")
    second = _event(db_session, received_at=same, outcome="duplicate")
    assert second.event_id > first.event_id

    listed = client.get("/api/properties/HISJ/intake-events", headers=headers).json()
    assert [e["event_id"] for e in listed["events"]] == [second.event_id, first.event_id]


@pytest.mark.parametrize("limit", ["0", "101", "-1"])
def test_an_out_of_range_limit_is_refused(world, limit):
    client, headers = world
    r = client.get(f"/api/properties/HISJ/intake-events?limit={limit}", headers=headers)
    assert r.status_code == 422, r.text


def test_a_local_part_always_fits_the_audit_resource_id(world, db_session):
    """What `intake_address_api._audit` rests on when it records the local
    part alone: a minted local part is never wider than the column. Asserted
    against the column's own declared width, so widening or narrowing
    `audit_event.resource_id` moves this test rather than silently changing
    what the trail can hold."""
    width = AuditEvent.__table__.c.resource_id.type.length
    assert width is not None
    for _ in range(20):
        assert len(intake.new_local_part()) <= width

    # And the row the endpoint actually writes carries it whole.
    client, headers = world
    made = client.post("/api/properties/HISJ/intake-address", headers=headers).json()
    assert _audits(db_session, "intake_address_created")[0].resource_id == \
        made["local_part"]


def test_a_rotation_that_loses_the_race_is_refused_with_409(
    world, db_engine, db_session, monkeypatch
):
    """Two rotations of the same address overlap: the other one commits
    between this request's read and its insert, and the partial unique
    refuses the insert. That is a 409 — a stale read — and never a 500.

    The race is made deterministic by completing the WINNER's rotation inside
    `_require_active`, in its own committed transaction: that is the exact
    window the request is exposed to, and the winner commits before this
    request's UPDATE, so neither transaction waits on the other.
    """
    client, headers = world
    client.post("/api/properties/HISJ/intake-address", headers=headers)
    real = intake_address_api._require_active

    def racing(session, property_id):
        row = real(session, property_id)
        monkeypatch.setattr(intake_address_api, "_require_active", real)  # once
        with make_session_factory(db_engine)() as winner:
            bind_org_context(winner, 1)
            live = winner.scalars(
                select(PropertyIntakeAddress).where(
                    PropertyIntakeAddress.property_id == property_id,
                    PropertyIntakeAddress.revoked_at.is_(None),
                )
            ).one()
            live.revoked_at = datetime.now(UTC)
            winner.flush()
            winner.add(PropertyIntakeAddress(
                local_part="na-thewinner", org_id=1, property_id=property_id))
            winner.commit()
        return row

    monkeypatch.setattr(intake_address_api, "_require_active", racing)
    r = client.post("/api/properties/HISJ/intake-address/rotate", headers=headers)
    assert r.status_code == 409, r.text
    assert "concurrently" in r.json()["detail"]

    db_session.expire_all()
    live = [row for row in _addresses(db_session) if row.revoked_at is None]
    assert [row.local_part for row in live] == ["na-thewinner"]
    # The loser wrote nothing at all, the audit trail included.
    assert _audits(db_session, "intake_address_rotated") == []
    assert _audits(db_session, "intake_address_revoked") == []


def test_a_local_part_collision_is_not_reported_as_a_conflict(
    db_engine, db_session, tmp_path, monkeypatch
):
    """`_violates` must discriminate, not just detect. A local part that
    collides with ANOTHER property's address breaks
    `uq_property_intake_address_local_part`, which this route has no answer
    for — 500 is the honest one, and answering 409 would tell the operator to
    rotate an address that is not the problem."""
    _org_and_property(db_session)
    _org_and_property(db_session, "SSSJ")
    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    headers = _admin_headers(mint, db_session)
    taken = client.post("/api/properties/SSSJ/intake-address",
                        headers=headers).json()["local_part"]

    monkeypatch.setattr(intake_address_api, "new_local_part", lambda: taken)
    r = client.post("/api/properties/HISJ/intake-address", headers=headers)
    assert r.status_code != 409, r.text
    assert r.status_code == 500
    assert _addresses(db_session, "HISJ") == []


# --- the gates ------------------------------------------------------------------


def test_a_department_manager_of_the_property_may_read_but_not_write(
    db_engine, db_session, tmp_path
):
    """The read gate and the write gate are different doors, and only the
    reads are open to every operator. A department manager AT THIS PROPERTY
    passes `_require_readable_property` and is still refused every write:
    minting or rotating an inbound capability is `require_config_writer`'s
    (ORG_ADMIN, PROPERTY_GM), exactly as `set_fiscal_calendar`'s is."""
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "department_manager", sub="dm-hisj", property_id="HISJ")
    tok = mint(roles=["department_manager"], sub="dm-hisj",
               scopes=[{"property_id": "HISJ", "department_id": None}])
    h = {"Authorization": f"Bearer {tok}"}

    assert client.post("/api/properties/HISJ/intake-address", headers=h).status_code == 403
    assert client.post("/api/properties/HISJ/intake-address/rotate",
                       headers=h).status_code == 403
    assert client.put("/api/properties/HISJ/intake-address",
                      json={"sender_domains": ["pms.test"]}, headers=h).status_code == 403
    assert _addresses(db_session) == []

    assert client.get("/api/properties/HISJ/intake-address", headers=h).status_code == 200
    assert client.get("/api/properties/HISJ/intake-events", headers=h).status_code == 200


def test_every_verb_confines_a_gm_of_another_property(db_engine, db_session, tmp_path):
    """A GM scoped to SSSJ is refused on all five verbs for HISJ — the same
    confinement `test_property_config_api.py::
    test_every_endpoint_confines_an_out_of_scope_gm` holds for the neighboring
    property-config routes. Confinement precedes existence: HISJ has no
    address at all here and the answer is still 403, never a 404 that would
    tell an outsider what does or does not exist."""
    _org_and_property(db_session)
    _org_and_property(db_session, "SSSJ")
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "property_gm", sub="gm-sss", property_id="SSSJ")
    tok = mint(roles=["property_gm"], sub="gm-sss",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    h = {"Authorization": f"Bearer {tok}"}

    assert client.get("/api/properties/HISJ/intake-address", headers=h).status_code == 403
    assert client.post("/api/properties/HISJ/intake-address", headers=h).status_code == 403
    assert client.post("/api/properties/HISJ/intake-address/rotate",
                       headers=h).status_code == 403
    assert client.put("/api/properties/HISJ/intake-address",
                      json={"sender_domains": ["pms.test"]}, headers=h).status_code == 403
    # Confinement precedes VALIDATION too: a body this route would refuse as
    # 422 must not be judged before the caller is, or the refusal an outsider
    # meets depends on what they sent.
    assert client.put("/api/properties/HISJ/intake-address",
                      json={"sender_domains": ["not a domain"]},
                      headers=h).status_code == 403
    assert client.get("/api/properties/HISJ/intake-events", headers=h).status_code == 403
    assert _addresses(db_session) == []


def test_an_org_two_operator_writes_its_own_org(
    two_tenant_world, db_session, db_url, tmp_path
):
    """The write side of the two-org world: org 2's admin, active in org 2,
    creates, rotates and scopes TWO1's address over the RLS-bound stack.

    Every row must land in org 2. `property_intake_address` is not OrgScoped,
    so nothing stamps `org_id` for it — the route reads
    `tenancy.current_org_id(session)`, the same predicate both walls read, and
    a literal here would either plant org 1's id (refused by
    `fk_property_intake_address_property_org`, since (1, TWO1) is no property)
    or, worse in another shape, quietly file one tenant's capability under
    another. The `audit_event` rows are the OrgScoped half of the same
    question.
    """
    w = two_tenant_world
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    token = mint(roles=["org_admin"], sub=w.org2_admin, organizations=[ORG2_ALIAS])
    h = {"Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: ORG2_ALIAS}

    created = client.post("/api/properties/TWO1/intake-address", headers=h)
    assert created.status_code == 201, created.text
    rotated = client.post("/api/properties/TWO1/intake-address/rotate", headers=h)
    assert rotated.status_code == 201, rotated.text
    scoped = client.put("/api/properties/TWO1/intake-address",
                        json={"sender_domains": ["pms.test"]}, headers=h)
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["sender_domains"] == ["pms.test"]

    # Read on the superuser session, which is bound to no org and therefore
    # sees every one of them — the org_id values below are the rows', not a
    # filter's.
    rows = _addresses(db_session, "TWO1")
    assert [row.local_part for row in rows] == [
        created.json()["local_part"], rotated.json()["local_part"]]
    assert {row.org_id for row in rows} == {w.org2_id}

    audits = list(db_session.scalars(
        select(AuditEvent).where(AuditEvent.resource_type == "property_intake_address")
        .order_by(AuditEvent.event_id)))
    assert [a.action for a in audits] == [
        "intake_address_created", "intake_address_revoked",
        "intake_address_rotated", "intake_sender_domains_set"]
    assert {a.org_id for a in audits} == {w.org2_id}


def test_an_address_of_another_org_is_invisible_and_unrotatable(
    two_tenant_world, db_session, db_url, tmp_path, app_role_engine
):
    """The pin `models.PropertyIntakeAddress` and `o1a0intake` both name.

    `property_intake_address` has NO RLS policy (D-OH23.3 — the webhook must
    resolve a local part before any org is known), so nothing under these
    routes filters it for us. Two legs, because there are two walls and only
    one of them is this module's:

    * over HTTP, org 1's admin — active in org 1, a member of both orgs — is
      refused on every verb for org 2's property. That refusal is the property
      gate's (`_require_onboardable_property` / `_require_readable_property`),
      which is why it is a 403 and not a 404: the same no-existence-oracle
      rule the property-config routes follow;
    * under it, `_active_address` on an ORG-1-BOUND session cannot see org 2's
      row at all, and on an org-2-bound session can. That is this module's own
      `org_id` filter, and it is what would still stand if a future route
      reached the table without a property id.
    """
    w = two_tenant_world
    db_session.add(PropertyIntakeAddress(
        local_part="na-orgtwo", org_id=w.org2_id, property_id="TWO1"))
    db_session.commit()

    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    token = mint(roles=["org_admin"], sub=ORG1_ADMIN,
                 organizations=[DEFAULT_ORG_ALIAS, ORG2_ALIAS])
    h = {"Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: DEFAULT_ORG_ALIAS}

    assert client.get("/api/properties/TWO1/intake-address", headers=h).status_code == 403
    assert client.post("/api/properties/TWO1/intake-address", headers=h).status_code == 403
    assert client.post("/api/properties/TWO1/intake-address/rotate",
                       headers=h).status_code == 403
    assert client.put("/api/properties/TWO1/intake-address",
                      json={"sender_domains": ["pms.test"]}, headers=h).status_code == 403
    assert client.get("/api/properties/TWO1/intake-events", headers=h).status_code == 403

    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, 1)
        assert _active_address(s, "TWO1") is None
    with factory() as s:
        bind_org_context(s, w.org2_id)
        found = _active_address(s, "TWO1")
        assert found is not None and found.local_part == "na-orgtwo"

    db_session.expire_all()
    rows = _addresses(db_session, "TWO1")
    assert [(r.local_part, r.revoked_at, r.sender_domains) for r in rows] == [
        ("na-orgtwo", None, None)]
