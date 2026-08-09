"""F6: unverified punches gate APPROVAL — with an audited acknowledgment.

Plan decision 3: match_state gates approval, never recording. `approve`
REFUSES a card holding `unverified` punches unless each one is explicitly
acknowledged in the approve call, and each acknowledgment writes its own
audit row (the "acknowledged-by"). The other states never gate:
`verified` obviously; `no_template` is the cold-start posture (grey — or
every approval would deadlock until the whole roster enrolls); NULL is a
pre-matching punch. Acknowledging a punch that is NOT an unverified punch
of THIS card is refused — the audit trail must never record an
acknowledgment that gated nothing.
"""

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    KioskDevice,
    Organization,
    Property,
    Punch,
    Timecard,
)
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.timecards import assemble_timecard
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    return TestClient(app)


def _card_with_punches(db_session, punches):
    """One HISJ employee, one card, punches as (type, hour, match_state,
    match_score) tuples on 2026-07-07."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="adm")
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H",
                        pay_type="hourly")
    db_session.add_all([device, emp])
    db_session.flush()
    ids = []
    for ptype, hour, state, score in punches:
        p = Punch(
            employee_id=emp.employee_id, kiosk_device_id=device.device_id,
            punch_type=ptype,
            punched_at=datetime(2026, 7, 7, hour, tzinfo=UTC),
            business_date=date(2026, 7, 7), photo_key=f"k/{ptype}{hour}",
            match_state=state, match_score=score,
        )
        db_session.add(p)
        db_session.flush()
        ids.append(p.punch_id)
    card = assemble_timecard(db_session, emp.employee_id, date(2026, 7, 7),
                             anchor=_ANCHOR)
    db_session.commit()
    # L4: the minted org_admin's authority is its DB grant, not the token.
    grant_role(db_session, "org_admin", sub="adm")
    return card.timecard_id, ids


def _approve(c, tok, card_id, body=None):
    return c.post(f"/api/timecards/{card_id}/approve", json=body,
                  headers={"Authorization": f"Bearer {tok}"})


def test_a_card_with_an_unverified_punch_refuses_bare_approval(
    db_engine, db_session, tmp_path
):
    card_id, [p_in, _] = _card_with_punches(db_session, [
        ("clock_in", 9, "unverified", 0.31),
        ("clock_out", 17, "verified", 0.94),
    ])
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")

    r = _approve(c, tok, card_id)
    assert r.status_code == 409
    assert str(p_in) in r.json()["detail"]
    assert db_session.get(Timecard, card_id).status == "open"

    # Acknowledging it approves — and the acknowledgment is audited per
    # punch, actor recorded.
    r = _approve(c, tok, card_id, {"acknowledged_punch_ids": [p_in]})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(Timecard, card_id).status == "approved"
    ack = db_session.execute(select(AuditEvent).where(
        AuditEvent.action == "acknowledge_unverified_punch")).scalar_one()
    assert ack.actor_subject == "adm"
    assert ack.resource_type == "punch"
    assert ack.resource_id == str(p_in)


def test_every_unverified_punch_must_be_acknowledged_not_just_one(
    db_engine, db_session, tmp_path
):
    card_id, [a, b] = _card_with_punches(db_session, [
        ("clock_in", 9, "unverified", 0.31),
        ("clock_out", 17, "unverified", None),
    ])
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")
    r = _approve(c, tok, card_id, {"acknowledged_punch_ids": [a]})
    assert r.status_code == 409
    assert str(b) in r.json()["detail"]
    assert db_session.get(Timecard, card_id).status == "open"


def test_verified_no_template_and_pre_matching_punches_never_gate(
    db_engine, db_session, tmp_path
):
    """no_template is the cold-start posture: gating on it would deadlock
    every approval until the whole roster enrolls. NULL is a pre-matching
    punch. Neither needs acknowledgment."""
    card_id, _ = _card_with_punches(db_session, [
        ("clock_in", 9, "verified", 0.91),
        ("lunch_start", 12, "no_template", None),
        ("lunch_end", 13, None, None),
        ("clock_out", 17, "verified", 0.88),
    ])
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = _approve(c, mint(roles=["org_admin"], sub="adm"), card_id)
    assert r.status_code == 200, r.text
    assert db_session.execute(select(AuditEvent).where(
        AuditEvent.action == "acknowledge_unverified_punch")).first() is None


def test_acknowledging_a_punch_that_gates_nothing_is_refused(
    db_engine, db_session, tmp_path
):
    """An acknowledgment row for a verified punch (or a stray id) would be
    a false audit record — refused before any state changes."""
    card_id, [p_verified] = _card_with_punches(db_session, [
        ("clock_in", 9, "verified", 0.94),
    ])
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")
    for bogus in ([p_verified], [999999]):
        r = _approve(c, tok, card_id, {"acknowledged_punch_ids": bogus})
        assert r.status_code == 422
        assert db_session.get(Timecard, card_id).status == "open"
    assert db_session.execute(select(AuditEvent).where(
        AuditEvent.action == "acknowledge_unverified_punch")).first() is None


def test_the_gate_is_scoped_to_this_cards_punches(
    db_engine, db_session, tmp_path
):
    """Another employee's unverified punch (a different card) must not
    block THIS card — and must not be acknowledgeable through it."""
    card_id, _ = _card_with_punches(db_session, [
        ("clock_in", 9, "verified", 0.94),
    ])
    other = make_employee(db_session, property_id="HISJ",
                          full_name="Olga O", pay_type="hourly")
    db_session.flush()
    p = Punch(
        employee_id=other.employee_id, kiosk_device_id=1,
        punch_type="clock_in",
        punched_at=datetime(2026, 7, 7, 9, tzinfo=UTC),
        business_date=date(2026, 7, 7), photo_key="k/other",
        match_state="unverified", match_score=0.2,
    )
    db_session.add(p)
    db_session.flush()
    assemble_timecard(db_session, other.employee_id, date(2026, 7, 7),
                      anchor=_ANCHOR)
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")
    assert _approve(c, tok, card_id).status_code == 200
    # And the other card's punch cannot be acknowledged via this card.
    db_session.execute(Timecard.__table__.update().where(
        Timecard.timecard_id == card_id).values(status="open"))
    db_session.commit()
    r = _approve(c, tok, card_id, {"acknowledged_punch_ids": [p.punch_id]})
    assert r.status_code == 422


def test_the_detail_view_carries_match_state_and_score(
    db_engine, db_session, tmp_path
):
    card_id, _ = _card_with_punches(db_session, [
        ("clock_in", 9, "unverified", 0.31),
        ("clock_out", 17, "verified", 0.94),
    ])
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get(f"/api/timecards/{card_id}",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    punches = [p for d in r.json()["days"] for p in d["punches"]]
    states = {p["punch_type"]: (p["match_state"], p["match_score"]) for p in punches}
    assert states["clock_in"] == ("unverified", 0.31)
    assert states["clock_out"] == ("verified", 0.94)
