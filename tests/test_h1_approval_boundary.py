"""H1: the approval boundary — early approval refuses, and a punch never
joins an approved card.

Decision 1: a period still in progress is not approvable. Approving a
period someone is still working in is the root cause under every
punch-onto-approved-card landing (the F8 deferral): the punch would join
a LOCKED card — bypassing the F6 acknowledgment gate (checked only at
approve time) and never promoting (promote runs once; re-approval 409s).
The refusal compares against the PROPERTY'S current business date
(night-audit cutoff respected), not the calendar date.

Decision 2: whatever still gets through the door (a backdated punch at
the cutoff boundary, an approval racing a punch) is RECORDED — wage
capture is never refused — but stays UNLINKED. Unlinked is a marker, not
a loss: H4's preflight guard names every unlinked punch, and H3's reopen
relinks them under a fresh review.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from usali.attendance import business_date_for
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import KioskDevice, Organization, Property, Punch, Timecard
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.timecards import assemble_timecard, period_in_progress
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_TZ = "America/Los_Angeles"


# --- the ONE predicate, unit-tested with pinned clocks -----------------------


def test_a_finished_period_is_not_in_progress():
    # 2026-07-20 12:00 LA time: business date 07-20; a period ending 07-19
    # is closed.
    now = datetime(2026, 7, 20, 12, 0, tzinfo=ZoneInfo(_TZ))
    assert period_in_progress(
        date(2026, 7, 19), now=now, timezone=_TZ, cutoff_hour=4
    ) is False


def test_the_periods_last_day_is_still_in_progress():
    # Business date == period_end: today's punches can still land on it.
    now = datetime(2026, 7, 19, 12, 0, tzinfo=ZoneInfo(_TZ))
    assert period_in_progress(
        date(2026, 7, 19), now=now, timezone=_TZ, cutoff_hour=4
    ) is True


def test_the_cutoff_hour_keeps_yesterday_in_progress():
    """03:59 LA on the 20th is still business date 07-19 (night audit):
    a closing shift is mid-punch — the period that ends 07-19 must not be
    approvable for another two minutes. At 04:01 it is."""
    before = datetime(2026, 7, 20, 3, 59, tzinfo=ZoneInfo(_TZ))
    after = datetime(2026, 7, 20, 4, 1, tzinfo=ZoneInfo(_TZ))
    assert period_in_progress(
        date(2026, 7, 19), now=before, timezone=_TZ, cutoff_hour=4
    ) is True
    assert period_in_progress(
        date(2026, 7, 19), now=after, timezone=_TZ, cutoff_hour=4
    ) is False


# --- the endpoint wires the predicate ----------------------------------------


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    return TestClient(app)


def _world(db_session, punch_dates):
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
    for day in punch_dates:
        for ptype, hour in (("clock_in", 17), ("clock_out", 23)):
            db_session.add(Punch(
                employee_id=emp.employee_id, kiosk_device_id=device.device_id,
                punch_type=ptype,
                punched_at=datetime(day.year, day.month, day.day, hour,
                                    tzinfo=UTC),
                business_date=day, photo_key=f"k/{ptype}{day.isoformat()}",
            ))
    db_session.flush()
    grant_role(db_session, "org_admin", sub="adm")  # L4: DB-backed authority
    return emp.employee_id, device.device_id


def test_approving_the_current_period_refuses_with_the_earliest_date(
    db_engine, db_session, tmp_path
):
    # TOMORROW's period, not today's: the test samples the business date
    # here and the endpoint samples it again — at the 4am boundary those
    # could straddle a day and close "today's" period between the two
    # reads (the recorded sampled-once class). A period containing
    # tomorrow is still in progress under either read.
    target = business_date_for(datetime.now(UTC), _TZ, cutoff_hour=4) \
        + timedelta(days=1)
    emp_id, _ = _world(db_session, [target])
    card = assemble_timecard(db_session, emp_id, target, anchor=_ANCHOR)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")

    r = c.post(f"/api/timecards/{card.timecard_id}/approve",
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "in progress" in detail
    earliest = card.period_end + timedelta(days=1)
    assert earliest.isoformat() in detail
    db_session.expire_all()
    assert db_session.get(Timecard, card.timecard_id).status == "open"


def test_a_closed_period_still_approves(db_engine, db_session, tmp_path):
    """The refusal must not over-block: a genuinely finished period
    approves exactly as before (the F6 suite approves 2026-07 cards; this
    pins the boundary here too)."""
    emp_id, _ = _world(db_session, [date(2026, 7, 7)])
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7),
                             anchor=_ANCHOR)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")

    r = c.post(f"/api/timecards/{card.timecard_id}/approve",
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text


# --- a punch never joins an approved card ------------------------------------


def test_a_punch_into_an_approved_period_stays_unlinked(db_session):
    """The card was approved with its punches reviewed; a punch landing in
    the same period afterwards must be RECORDED but not join the locked
    card — joining silently is the F8 bypass (unreviewed verdict, minutes
    that never promote). The earlier punches keep their link."""
    emp_id, device_id = _world(db_session, [date(2026, 7, 7)])
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7),
                             anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()

    late = Punch(
        employee_id=emp_id, kiosk_device_id=device_id,
        punch_type="clock_in",
        punched_at=datetime(2026, 7, 8, 17, tzinfo=UTC),
        business_date=date(2026, 7, 8), photo_key="k/late",
    )
    db_session.add(late)
    db_session.flush()
    returned = assemble_timecard(db_session, emp_id, date(2026, 7, 8),
                                 anchor=_ANCHOR)
    db_session.commit()

    assert returned.timecard_id == card.timecard_id  # same period, same card
    db_session.refresh(late)
    assert late.timecard_id is None, "a punch joined an APPROVED card"
    linked = db_session.execute(
        select(Punch).where(Punch.timecard_id == card.timecard_id)
    ).scalars().all()
    assert len(linked) == 2  # the reviewed originals, exactly
