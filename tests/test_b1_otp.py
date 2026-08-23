"""OtpService: issue/verify happy path; wrong/expired/exhausted fail closed;
the code is stored hashed, never in the clear."""

from datetime import datetime, timedelta, timezone

from usali.otp import OtpService
from usali.models import OtpChallenge
from sqlalchemy import select

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
_PURPOSE = "signup_cell"
_TARGET = "+15550000000"  # synthetic


def test_issue_then_verify_succeeds_once(db_session):
    svc = OtpService()
    code = svc.issue(db_session, purpose=_PURPOSE, target=_TARGET, now=_NOW)
    db_session.commit()
    assert code.isdigit() and len(code) == 6
    row = db_session.execute(select(OtpChallenge)).scalar_one()
    assert row.code_hash != code
    assert svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                      code=code, now=_NOW + timedelta(minutes=1))
    db_session.commit()
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code=code, now=_NOW + timedelta(minutes=1))


def test_wrong_code_fails_and_counts_against_the_attempt_limit(db_session):
    svc = OtpService(max_attempts=3)
    code = svc.issue(db_session, purpose=_PURPOSE, target=_TARGET, now=_NOW)
    db_session.commit()
    for _ in range(3):
        assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                              code="000000", now=_NOW)
        db_session.commit()
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code=code, now=_NOW)


def test_expired_code_fails_closed(db_session):
    svc = OtpService(ttl=timedelta(minutes=5))
    code = svc.issue(db_session, purpose=_PURPOSE, target=_TARGET, now=_NOW)
    db_session.commit()
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code=code, now=_NOW + timedelta(minutes=6))


def test_verify_with_no_challenge_fails_closed(db_session):
    svc = OtpService()
    assert not svc.verify(db_session, purpose=_PURPOSE, target=_TARGET,
                          code="123456", now=_NOW)
