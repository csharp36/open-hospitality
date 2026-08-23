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


def test_claim_wins_once_then_loses(db_session):
    _, raw = invites.create_invite(db_session, "e@example.test")
    db_session.commit()
    assert invites.claim(db_session, raw) is True     # first wins
    db_session.commit()
    assert invites.claim(db_session, raw) is False    # already consumed -> loses
    db_session.commit()


def test_claim_loses_on_unknown_or_expired(db_session):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    _, raw = invites.create_invite(db_session, "f@example.test",
                                   ttl=timedelta(hours=1), now=now)
    db_session.commit()
    assert invites.claim(db_session, "no-such-token", now=now) is False
    assert invites.claim(db_session, raw, now=now + timedelta(hours=2)) is False  # expired


def test_revert_claim_returns_it_to_pending(db_session):
    _, raw = invites.create_invite(db_session, "g@example.test")
    db_session.commit()
    assert invites.claim(db_session, raw) is True
    db_session.commit()
    invites.revert_claim(db_session, raw)
    db_session.commit()
    assert invites.validate(db_session, raw) is not None  # pending again, retryable


def test_mark_consumed_org_records_the_tenant(db_session):
    ensure_default_org(db_session)
    invite, raw = invites.create_invite(db_session, "h@example.test")
    db_session.commit()
    invites.claim(db_session, raw)
    invites.mark_consumed_org(db_session, raw, org_id=1)
    db_session.commit()
    db_session.refresh(invite)
    assert invite.status == "consumed" and invite.consumed_org_id == 1
