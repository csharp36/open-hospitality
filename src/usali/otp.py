"""OTP challenge service (Track B/B1, folded in per D-B6). Numeric codes,
hashed at rest, expiring, attempt-limited, and single-use. Every failure mode —
no challenge, wrong code, expired, exhausted — returns False (fail-closed); the
caller refuses without saying which. Does NOT commit; the caller owns the
transaction so an attempt increment is durable even when it later refuses."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import OtpChallenge

_DEFAULT_TTL = timedelta(minutes=10)
_DEFAULT_MAX_ATTEMPTS = 5


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OtpService:
    def __init__(
        self,
        *,
        ttl: timedelta = _DEFAULT_TTL,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._ttl = ttl
        self._max_attempts = max_attempts

    def issue(
        self, session: Session, *, purpose: str, target: str,
        now: datetime | None = None,
    ) -> str:
        """Mint a fresh 6-digit code for (purpose, target), superseding any
        prior outstanding challenge for the same pair. Returns the raw code (to
        be sent via the Notifier); only its hash is stored."""
        moment = now or _now()
        for stale in session.execute(
            select(OtpChallenge).where(
                OtpChallenge.purpose == purpose, OtpChallenge.target == target
            )
        ).scalars():
            session.delete(stale)
        code = f"{secrets.randbelow(1_000_000):06d}"
        session.add(OtpChallenge(
            purpose=purpose, target=target, code_hash=_hash_code(code),
            expires_at=moment + self._ttl, attempts=0,
        ))
        session.flush()
        return code

    def verify(
        self, session: Session, *, purpose: str, target: str, code: str,
        now: datetime | None = None,
    ) -> bool:
        """True iff (purpose, target) has a live, unexhausted challenge whose
        code matches. On a wrong code, increments attempts (fail-closed on
        exhaustion). On success, consumes the challenge (single-use)."""
        moment = now or _now()
        challenge = session.execute(
            select(OtpChallenge).where(
                OtpChallenge.purpose == purpose, OtpChallenge.target == target
            )
        ).scalar_one_or_none()
        if challenge is None:
            return False
        if challenge.expires_at <= moment or challenge.attempts >= self._max_attempts:
            return False
        if not hmac.compare_digest(challenge.code_hash, _hash_code(code)):
            challenge.attempts += 1
            session.flush()
            return False
        session.delete(challenge)  # single-use
        session.flush()
        return True
