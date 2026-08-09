"""H3: reopen — an explicit, authorized, audited act, never a side effect.

Decision 3: the resolution path for an unlinked punch (H1's marker) is a
human reopening the card, not the punch un-approving anything — a kiosk
device token must never be able to undo a GM's approval. Reopen unwinds
exactly what approval did: status back to open, approval identity
cleared, the card's estimated labor facts deleted (promotion is
delete-then-rewrite keyed on timecard_id, so re-approval re-promotes),
and the period's unlinked punches relinked under the fresh review.
Re-approval re-runs the F6 gate over ALL punches, including the relinked
ones — acknowledgments do not carry over.

Reopen is allowed even when the period's pay run already submitted: the
run's PayRunLine snapshot is deliberately immutable paid history, so
reopening desyncs nothing — the extra minutes surface through H4's
preflight guard, not through mutation of what was paid.
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
    PayRun,
    Property,
    Punch,
    Timecard,
    UsaliLaborFact,
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
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "accountant", sub="acct")
    return emp.employee_id, device.device_id


def _late_punch(db_session, emp_id, device_id, day, *, match_state=None):
    punch = Punch(
        employee_id=emp_id, kiosk_device_id=device_id,
        punch_type="clock_in",
        punched_at=datetime(day.year, day.month, day.day, 17, tzinfo=UTC),
        business_date=day, photo_key=f"k/late{day.isoformat()}",
        match_state=match_state,
        match_score=0.31 if match_state == "unverified" else None,
    )
    db_session.add(punch)
    out = Punch(
        employee_id=emp_id, kiosk_device_id=device_id,
        punch_type="clock_out",
        punched_at=datetime(day.year, day.month, day.day, 21, tzinfo=UTC),
        business_date=day, photo_key=f"k/lateout{day.isoformat()}",
    )
    db_session.add(out)
    db_session.flush()
    return punch


def _approved_world(db_session, db_engine, tmp_path):
    """A card for the CLOSED period 2026-07-06..19, approved through the real
    endpoint (so its labor facts are promoted), plus the client + tokens."""
    emp_id, device_id = _world(db_session, [date(2026, 7, 7)])
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7),
                             anchor=_ANCHOR)
    db_session.commit()
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")
    r = client.post(f"/api/timecards/{card.timecard_id}/approve",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    return emp_id, device_id, card.timecard_id, client, mint, tok


def _facts(db_session, timecard_id):
    return db_session.execute(
        select(UsaliLaborFact).where(UsaliLaborFact.timecard_id == timecard_id)
    ).scalars().all()


def test_reopen_flips_relinks_and_deletes_the_facts(
    db_engine, db_session, tmp_path
):
    """The full unwind in one act: status open, approval identity CLEARED
    (a card claiming an approver it no longer has would mislead every
    audit read), the estimated facts GONE (a reopened card's hours are
    under review again — they must not keep claiming approved cost on
    reports), the unlinked punch RELINKED (the fresh review must see it),
    and an audit row naming who did it."""
    emp_id, device_id, card_id, client, _mint, tok = _approved_world(
        db_session, db_engine, tmp_path
    )
    assert _facts(db_session, card_id), "approval must have promoted facts"
    late = _late_punch(db_session, emp_id, device_id, date(2026, 7, 8))
    assemble_timecard(db_session, emp_id, date(2026, 7, 8), anchor=_ANCHOR)
    db_session.commit()
    db_session.refresh(late)
    assert late.timecard_id is None  # the H1 marker this endpoint resolves

    r = client.post(f"/api/timecards/{card_id}/reopen",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"

    db_session.expire_all()
    card = db_session.get(Timecard, card_id)
    assert card is not None
    assert card.status == "open"
    assert card.approved_by is None
    assert card.approved_at is None
    assert _facts(db_session, card_id) == []
    db_session.refresh(late)
    assert late.timecard_id == card_id, "reopen must relink the marker punch"
    audit = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "reopen_timecard")
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].resource_id == str(card_id)
    assert audit[0].actor_subject == "adm"


def test_reopen_requires_an_approver(db_engine, db_session, tmp_path):
    """Approver-scoped is the whole point of decision 3: reopen undoes a
    GM's approval, so only someone holding approval authority may do it —
    never a viewer role, and (by construction: this router only speaks
    bearer principals) never a kiosk device token."""
    _emp, _dev, card_id, client, mint, _tok = _approved_world(
        db_session, db_engine, tmp_path
    )
    viewer = mint(roles=["accountant"], sub="acct")
    r = client.post(f"/api/timecards/{card_id}/reopen",
                    headers={"Authorization": f"Bearer {viewer}"})
    assert r.status_code == 403
    db_session.expire_all()
    card = db_session.get(Timecard, card_id)
    assert card is not None
    assert card.status == "approved", "a refused reopen must change nothing"


def test_reopening_an_open_card_409s(db_engine, db_session, tmp_path):
    emp_id, _dev = _world(db_session, [date(2026, 7, 7)])
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7),
                             anchor=_ANCHOR)
    db_session.commit()
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub="adm")
    r = client.post(f"/api/timecards/{card.timecard_id}/reopen",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 409
    assert "approved" in r.json()["detail"]


def test_reapproval_re_runs_the_f6_gate_over_the_relinked_punch(
    db_engine, db_session, tmp_path
):
    """The relinked punch is UNREVIEWED — its red badge was never in front
    of the approver who signed the first time. Re-approval must refuse
    until it is explicitly acknowledged (acknowledgments do not carry
    over), and the re-promotion must then cover the new day's minutes —
    the exact hours the F8 bypass used to silently drop."""
    emp_id, device_id, card_id, client, _mint, tok = _approved_world(
        db_session, db_engine, tmp_path
    )
    red = _late_punch(db_session, emp_id, device_id, date(2026, 7, 8),
                      match_state="unverified")
    assemble_timecard(db_session, emp_id, date(2026, 7, 8), anchor=_ANCHOR)
    db_session.commit()

    r = client.post(f"/api/timecards/{card_id}/reopen",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text

    bare = client.post(f"/api/timecards/{card_id}/approve",
                       headers={"Authorization": f"Bearer {tok}"})
    assert bare.status_code == 409
    assert str(red.punch_id) in bare.json()["detail"]

    acked = client.post(
        f"/api/timecards/{card_id}/approve",
        headers={"Authorization": f"Bearer {tok}"},
        json={"acknowledged_punch_ids": [red.punch_id]},
    )
    assert acked.status_code == 200, acked.text
    db_session.expire_all()
    fact_days = {f.business_date for f in _facts(db_session, card_id)}
    assert fact_days == {date(2026, 7, 7), date(2026, 7, 8)}, (
        "re-promotion must cover the relinked day's minutes"
    )


def test_reopen_is_allowed_after_the_pay_run_submitted(
    db_engine, db_session, tmp_path
):
    """Deliberate (decision 3): the run's PayRunLine snapshot is immutable
    paid history, so reopening desyncs nothing — refusing here would leave
    the unlinked punch permanently unresolvable. The run itself must ride
    untouched."""
    _emp, _dev, card_id, client, _mint, tok = _approved_world(
        db_session, db_engine, tmp_path
    )
    db_session.add(PayRun(
        property_id="HISJ", period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 19), check_date=date(2026, 7, 24),
        status="submitted", provider="gusto", created_by="adm",
    ))
    db_session.commit()

    r = client.post(f"/api/timecards/{card_id}/reopen",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    run = db_session.execute(select(PayRun)).scalars().one()
    assert run.status == "submitted", "paid history must ride untouched"
