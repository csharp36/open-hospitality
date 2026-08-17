"""Invite lifecycle: create -> valid -> consume -> invalid-on-reuse; expiry;
revoke. The raw token is never stored — only its SHA-256."""

from datetime import datetime, timedelta, timezone

from usali import invites
from usali.mapping.property_registry import ensure_default_org

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def test_create_returns_raw_token_and_stores_only_the_hash(db_session):
    invite, raw = invites.create_invite(db_session, "owner@example.test", now=_NOW)
    db_session.commit()
    assert raw and invite.token_hash != raw
    assert invites._hash_token(raw) == invite.token_hash
    assert invite.status == "pending"
    assert invite.email == "owner@example.test"


def test_validate_accepts_pending_unexpired_matching(db_session):
    _, raw = invites.create_invite(db_session, "a@example.test", now=_NOW)
    db_session.commit()
    found = invites.validate(db_session, raw, now=_NOW + timedelta(hours=1))
    assert found is not None and found.email == "a@example.test"


def test_validate_rejects_unknown_expired_and_consumed(db_session):
    # consume() below records org_id=1 via a real FK to organization, so the
    # founding org must exist first — the db_session fixture truncates all
    # tables per test and does not auto-seed one (mirrors the ensure_default_org
    # call in test_consume_marks_status_and_records_org).
    ensure_default_org(db_session)
    invite, raw = invites.create_invite(
        db_session, "b@example.test", ttl=timedelta(hours=1), now=_NOW
    )
    db_session.commit()
    assert invites.validate(db_session, "not-a-real-token", now=_NOW) is None
    assert invites.validate(db_session, raw, now=_NOW + timedelta(hours=2)) is None
    invites.consume(db_session, invite, org_id=1)
    db_session.commit()
    assert invites.validate(db_session, raw, now=_NOW + timedelta(minutes=5)) is None


def test_consume_marks_status_and_records_org(db_session):
    ensure_default_org(db_session)
    invite, raw = invites.create_invite(db_session, "c@example.test", now=_NOW)
    db_session.commit()
    invites.consume(db_session, invite, org_id=1)
    db_session.commit()
    assert invite.status == "consumed"
    assert invite.consumed_org_id == 1


def test_revoke_makes_it_invalid(db_session):
    invite, raw = invites.create_invite(db_session, "d@example.test", now=_NOW)
    db_session.commit()
    invites.revoke(db_session, invite)
    db_session.commit()
    assert invites.validate(db_session, raw, now=_NOW) is None
