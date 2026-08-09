"""Kiosk my-week (Pillar D2): an employee taps their name and sees THEIR week
from the latest PUBLISHED schedule. Device-token auth, property-confined,
drafts invisible, no money, no other employees' names."""

from datetime import UTC, date, datetime, time

from fastapi.testclient import TestClient

from sqlalchemy import select

from tests.employees import make_employee
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.kiosk import mint_device_token
from usali.models import (
    AuditEvent,
    Department,
    Employee,
    KioskDevice,
    Organization,
    Property,
    Schedule,
    Shift,
)
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from tests.authkit import make_authkit
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

WEEK = date(2026, 7, 20)  # a Monday


def _app(db_engine, tmp_path):
    return create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )


def _seed(db_session, status="published"):
    """Two properties, one enrolled HISJ kiosk, two HISJ employees + one SSSJ
    employee, and one HISJ schedule for WEEK with shifts for BOTH HISJ staff."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.flush()
    front = Department(property_id="HISJ", name="Front Desk")
    house = Department(property_id="HISJ", name="Housekeeping")
    db_session.add_all([front, house])
    db_session.flush()
    token, token_hash = mint_device_token()
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash=token_hash,
                         enrolled_by="adm")
    # The tapped employee carries a pay rate — the response must NEVER echo it.
    hank = make_employee(db_session, property_id="HISJ", full_name="Hank HISJ", pay_type="hourly",
                    pay_rate="27.50")
    wanda = make_employee(db_session, property_id="HISJ", full_name="Wanda Other", pay_type="hourly")
    sam = make_employee(db_session, property_id="SSSJ", full_name="Sam SSSJ", pay_type="hourly")
    db_session.add_all([device, hank, wanda, sam])
    db_session.flush()
    sched = Schedule(property_id="HISJ", week_start=WEEK, status=status,
                     version=1 if status == "published" else 0,
                     published_by="gm" if status == "published" else None,
                     published_at=datetime.now(UTC) if status == "published" else None)
    db_session.add(sched)
    db_session.flush()
    db_session.add_all([
        Shift(schedule_id=sched.schedule_id, business_date=WEEK,
              department_id=front.department_id, start_time=time(7, 0),
              end_time=time(15, 0), crosses_midnight=False,
              employee_id=hank.employee_id),
        Shift(schedule_id=sched.schedule_id, business_date=date(2026, 7, 22),
              department_id=front.department_id, start_time=time(23, 0),
              end_time=time(7, 0), crosses_midnight=True,
              employee_id=hank.employee_id),
        # Wanda's shift — must NEVER appear in Hank's week.
        Shift(schedule_id=sched.schedule_id, business_date=WEEK,
              department_id=house.department_id, start_time=time(9, 30),
              end_time=time(17, 30), crosses_midnight=False,
              employee_id=wanda.employee_id),
    ])
    db_session.commit()
    return token, hank.employee_id, wanda.employee_id, sam.employee_id


def _get(client, token, employee_id, week_start=WEEK):
    return client.get(
        "/api/kiosk/my-week",
        params={"employee_id": employee_id, "week_start": week_start.isoformat()},
        headers={"X-Kiosk-Token": token},
    )


def test_my_week_returns_own_shifts_from_the_published_schedule(
    db_engine, db_session, tmp_path
):
    token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 200
    body = r.json()
    assert body["published"] is True
    assert body["shifts"] == [
        {"business_date": "2026-07-20", "department": "Front Desk",
         "start_time": "07:00", "end_time": "15:00", "crosses_midnight": False},
        {"business_date": "2026-07-22", "department": "Front Desk",
         "start_time": "23:00", "end_time": "07:00", "crosses_midnight": True},
    ]


def test_another_employees_shifts_and_name_are_absent(db_engine, db_session, tmp_path):
    token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 200
    assert "Wanda" not in r.text  # no other employee's name
    assert "Housekeeping" not in r.text  # no other employee's shift
    assert "09:30" not in r.text


def test_draft_schedule_is_invisible_to_the_kiosk(db_engine, db_session, tmp_path):
    """THE test: an unpublished week must look like no schedule at all."""
    token, hank, _wanda, _sam = _seed(db_session, status="draft")
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 200
    body = r.json()
    assert body["published"] is False
    assert body["shifts"] == []
    assert "Front Desk" not in r.text  # no draft content leaks


def test_week_with_no_schedule_is_empty_and_unpublished(db_engine, db_session, tmp_path):
    token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank, week_start=date(2026, 7, 27))
    assert r.status_code == 200
    assert r.json() == {
        "employee_id": hank, "week_start": "2026-07-27",
        "published": False, "shifts": [],
    }


def test_another_propertys_employee_is_the_single_403(db_engine, db_session, tmp_path):
    """A HISJ kiosk asking about a SSSJ employee learns nothing — the punch
    endpoint's oracle collapse: one 403, indistinguishable from an unknown or
    terminated id."""
    token, _hank, _wanda, sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, sam)
    assert r.status_code == 403
    assert "Sam" not in r.text


def test_unknown_employee_is_the_single_403(db_engine, db_session, tmp_path):
    token, _hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, 999999)
    assert r.status_code == 403


def test_terminated_employee_is_the_single_403(db_engine, db_session, tmp_path):
    token, hank, _wanda, _sam = _seed(db_session)
    emp = db_session.get(Employee, hank)
    emp.termination_date = date(2026, 7, 1)
    emp.employment_status = "terminated"
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 403


def test_all_three_denials_share_one_status_and_detail(db_engine, db_session, tmp_path):
    """The status oracle is collapsed: unknown id, another property's id, and a
    terminated id are byte-identical — the device cannot triangulate which."""
    token, hank, _wanda, sam = _seed(db_session)
    emp = db_session.get(Employee, hank)
    emp.termination_date = date(2026, 7, 1)
    emp.employment_status = "terminated"
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path))
    responses = [_get(c, token, eid) for eid in (999999, sam, hank)]
    assert [r.status_code for r in responses] == [403, 403, 403]
    assert len({r.text for r in responses}) == 1


def test_missing_token_is_401(db_engine, db_session, tmp_path):
    _token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = c.get("/api/kiosk/my-week",
              params={"employee_id": hank, "week_start": WEEK.isoformat()})
    assert r.status_code == 401


def test_unknown_token_is_401(db_engine, db_session, tmp_path):
    _token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, "not-a-real-token", hank)
    assert r.status_code == 401


def test_revoked_device_is_403(db_engine, db_session, tmp_path):
    token, hank, _wanda, _sam = _seed(db_session)
    device = db_session.query(KioskDevice).one()
    device.revoked_at = datetime.now(UTC)
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 403


def test_response_carries_no_money(db_engine, db_session, tmp_path):
    """The kiosk surface is money-free by design — the tapped employee's own
    encrypted rate must never surface, nor any cost/rate key."""
    token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 200
    assert "27.50" not in r.text
    assert "pay_rate" not in r.text
    assert "rate" not in r.text
    assert "cost" not in r.text
    assert "$" not in r.text


def _my_week_audits(db_session):
    return db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "kiosk_my_week")
    ).scalars().all()


def test_successful_read_writes_one_audit_row(db_engine, db_session, tmp_path):
    """my-week is a silent read — unlike a punch it leaves no row of its own,
    so schedule enumeration from the lobby must leave an audit trail. The
    device IS the actor: there is no principal on the kiosk router."""
    token, hank, _wanda, _sam = _seed(db_session)
    device_id = db_session.execute(select(KioskDevice)).scalar_one().device_id
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 200
    audits = _my_week_audits(db_session)
    assert len(audits) == 1
    assert audits[0].actor_subject == f"kiosk:{device_id}"
    assert audits[0].action == "kiosk_my_week"
    assert audits[0].resource_type == "employee"
    assert audits[0].resource_id == str(hank)


def test_second_read_audits_again(db_engine, db_session, tmp_path):
    token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    assert _get(c, token, hank).status_code == 200
    assert _get(c, token, hank).status_code == 200
    assert len(_my_week_audits(db_session)) == 2


def test_unpublished_week_is_a_successful_read_and_audited(
    db_engine, db_session, tmp_path
):
    """`published: false` is still an egress — the device learned that no
    published schedule exists. Audited like any successful read."""
    token, hank, _wanda, _sam = _seed(db_session, status="draft")
    c = TestClient(_app(db_engine, tmp_path))
    r = _get(c, token, hank)
    assert r.status_code == 200
    assert r.json()["published"] is False
    assert len(_my_week_audits(db_session)) == 1


def test_denied_reads_are_not_audited(db_engine, db_session, tmp_path):
    """The egress-only house rule: denials leave no audit row. 401 (missing
    and unknown token), 403 (out-of-scope employee), and revoked-device 403
    all stay silent."""
    token, hank, _wanda, sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    # 401: missing token, then unknown token.
    r = c.get("/api/kiosk/my-week",
              params={"employee_id": hank, "week_start": WEEK.isoformat()})
    assert r.status_code == 401
    assert _get(c, "not-a-real-token", hank).status_code == 401
    # 403: unknown id, cross-property id.
    assert _get(c, token, 999999).status_code == 403
    assert _get(c, token, sam).status_code == 403
    # 403: revoked device.
    device = db_session.execute(select(KioskDevice)).scalar_one()
    device.revoked_at = datetime.now(UTC)
    db_session.commit()
    assert _get(c, token, hank).status_code == 403
    assert _my_week_audits(db_session) == []


def test_j5_the_my_week_payload_is_pinned_unchanged(
    db_engine, db_session, tmp_path
):
    """THE J5 pin: CRM demand landed on the scheduler surfaces and the
    kiosk sees NONE of it (plan decision 4 — my-week is an employee
    surface; demand, and above all its labels, is manager working data).
    The exact key sets are pinned so any future field rides in only by
    editing this test with intent."""
    token, hank, _wanda, _sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    body = _get(c, token, hank).json()
    assert set(body) == {"employee_id", "week_start", "published", "shifts"}
    for shift in body["shifts"]:
        assert set(shift) == {
            "business_date", "department", "start_time", "end_time",
            "crosses_midnight",
        }
