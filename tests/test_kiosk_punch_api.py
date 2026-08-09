from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.employees import make_employee
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.kiosk import mint_device_token
from usali.models import Employee, KioskDevice, Organization, Property, Punch, Timecard
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from tests.authkit import make_authkit
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _app(db_engine, tmp_path, store):
    return create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=store,
    )


def _seed(db_session):
    """Two properties, one enrolled HISJ kiosk, one employee at each property."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.flush()
    token, token_hash = mint_device_token()
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash=token_hash,
                         enrolled_by="adm")
    hisj = make_employee(db_session, property_id="HISJ", full_name="Hank HISJ", pay_type="hourly")
    sssj = make_employee(db_session, property_id="SSSJ", full_name="Sam SSSJ", pay_type="hourly")
    db_session.add_all([device, hisj, sssj])
    db_session.commit()
    return token, device.device_id, hisj.employee_id, sssj.employee_id


def _photo():
    return {"photo": ("p.jpg", b"\xff\xd8\xff\xe0 fake", "image/jpeg")}


def test_punch_requires_a_device_token(db_engine, db_session, tmp_path):
    _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": 1, "punch_type": "clock_in"},
               files=_photo())
    assert r.status_code == 401


def test_unknown_token_rejected(db_engine, db_session, tmp_path):
    _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": 1, "punch_type": "clock_in"},
               files=_photo(), headers={"X-Kiosk-Token": "not-a-real-token"})
    assert r.status_code == 401


def test_revoked_device_rejected(db_engine, db_session, tmp_path):
    token, device_id, emp_id, _ = _seed(db_session)
    device = db_session.get(KioskDevice, device_id)
    device.revoked_at = datetime.now(UTC)
    db_session.commit()

    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": emp_id, "punch_type": "clock_in"},
               files=_photo(), headers={"X-Kiosk-Token": token})
    assert r.status_code == 403


def test_device_cannot_punch_another_propertys_employee(db_engine, db_session, tmp_path):
    token, _device_id, _hisj, sssj = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": sssj, "punch_type": "clock_in"},
               files=_photo(), headers={"X-Kiosk-Token": token})
    assert r.status_code == 403


def test_punch_stores_row_and_photo(db_engine, db_session, tmp_path):
    token, device_id, hisj, _ = _seed(db_session)
    store = InMemoryPhotoStore()
    c = TestClient(_app(db_engine, tmp_path, store))
    r = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
               files=_photo(), headers={"X-Kiosk-Token": token})
    assert r.status_code == 201
    body = r.json()
    assert body["punch_type"] == "clock_in"
    assert body["business_date"]

    punch = db_session.execute(select(Punch)).scalars().one()
    assert punch.employee_id == hisj
    assert punch.kiosk_device_id == device_id
    assert punch.photo_key is not None
    assert store.get(punch.photo_key) == b"\xff\xd8\xff\xe0 fake"  # the photo landed
    assert punch.business_date == date.fromisoformat(body["business_date"])


def test_bad_punch_type_is_422(db_engine, db_session, tmp_path):
    token, _d, hisj, _ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "teleport"},
               files=_photo(), headers={"X-Kiosk-Token": token})
    assert r.status_code == 422


def test_roster_lists_only_this_propertys_active_employees(db_engine, db_session, tmp_path):
    token, _d, _h, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.get("/api/kiosk/employees", headers={"X-Kiosk-Token": token})
    assert r.status_code == 200
    names = {e["full_name"] for e in r.json()}
    assert names == {"Hank HISJ"}  # SSSJ employee not visible to a HISJ kiosk


def test_terminated_employee_cannot_punch(db_engine, db_session, tmp_path):
    from datetime import date as _date
    token, _d, hisj, _s = _seed(db_session)
    emp = db_session.get(Employee, hisj)
    emp.termination_date = _date(2026, 7, 1)
    emp.employment_status = "terminated"
    db_session.commit()

    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
               files=_photo(), headers={"X-Kiosk-Token": token})
    assert r.status_code == 403
    assert db_session.execute(select(Punch)).scalars().all() == []  # no hours minted


def test_oversized_photo_rejected(db_engine, db_session, tmp_path):
    token, _d, hisj, _s = _seed(db_session)
    big = b"\xff\xd8\xff" + b"x" * (2 * 1024 * 1024 + 10)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
               files={"photo": ("p.jpg", big, "image/jpeg")},
               headers={"X-Kiosk-Token": token})
    assert r.status_code == 413
    assert db_session.execute(select(Punch)).scalars().all() == []


def test_non_jpeg_photo_rejected(db_engine, db_session, tmp_path):
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
               files={"photo": ("p.exe", b"MZ not a jpeg", "image/jpeg")},
               headers={"X-Kiosk-Token": token})
    assert r.status_code == 422
    assert db_session.execute(select(Punch)).scalars().all() == []


def test_double_tap_same_punch_type_is_rejected(db_engine, db_session, tmp_path):
    """A double-tap must not mint two clock_ins (it would silently corrupt hours)."""
    token, _d, hisj, _s = _seed(db_session)
    store = InMemoryPhotoStore()
    c = TestClient(_app(db_engine, tmp_path, store))
    hdr = {"X-Kiosk-Token": token}
    first = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
                   files=_photo(), headers=hdr)
    assert first.status_code == 201
    second = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
                    files=_photo(), headers=hdr)
    assert second.status_code == 409
    assert len(db_session.execute(select(Punch)).scalars().all()) == 1
    # The 409 must fire BEFORE the photo is read/stored — a rejected double-tap
    # leaves no orphaned face image behind.
    assert len(store.keys()) == 1


def test_a_different_punch_type_is_not_debounced(db_engine, db_session, tmp_path):
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    hdr = {"X-Kiosk-Token": token}
    c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
           files=_photo(), headers=hdr)
    r = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "lunch_start"},
               files=_photo(), headers=hdr)
    assert r.status_code == 201
    assert len(db_session.execute(select(Punch)).scalars().all()) == 2


def test_punch_assembles_a_timecard_and_links_itself(db_engine, db_session, tmp_path):
    """A successful punch must materialize the employee's card for its period and
    link itself to it — otherwise the review queue is empty and the photo purge
    (which joins on punch.timecard_id) never runs."""
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    hdr = {"X-Kiosk-Token": token}
    r = c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
               files=_photo(), headers=hdr)
    assert r.status_code == 201

    punch = db_session.execute(select(Punch)).scalars().one()
    card = db_session.execute(select(Timecard)).scalars().one()
    assert card.employee_id == hisj
    assert card.period_start <= punch.business_date <= card.period_end
    assert punch.timecard_id == card.timecard_id


def test_two_punches_same_period_link_to_the_same_card(db_engine, db_session, tmp_path):
    """Assembly is idempotent: a second punch in the same period reuses the one
    card, it does not mint a new one."""
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    hdr = {"X-Kiosk-Token": token}
    c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "clock_in"},
           files=_photo(), headers=hdr)
    # A different punch type dodges the double-tap debounce but stays in-period.
    c.post("/api/kiosk/punch", data={"employee_id": hisj, "punch_type": "lunch_start"},
           files=_photo(), headers=hdr)

    cards = db_session.execute(select(Timecard)).scalars().all()
    assert len(cards) == 1
    punches = db_session.execute(select(Punch)).scalars().all()
    assert len(punches) == 2
    assert {p.timecard_id for p in punches} == {cards[0].timecard_id}


# --- punch ORDER ---------------------------------------------------------------
# Until this rule existed the endpoint took any known punch_type in any order, so
# a device could start a lunch for someone who never clocked in, or clock out
# mid-lunch — and that last one is not cosmetic: the lunch never closes, so
# `compute_day` deducts nothing and the employee is paid straight through it.


def _punch(c, token, employee_id, punch_type):
    return c.post(
        "/api/kiosk/punch",
        data={"employee_id": employee_id, "punch_type": punch_type},
        files=_photo(),
        headers={"X-Kiosk-Token": token},
    )


def test_lunch_start_without_clocking_in_is_refused(db_engine, db_session, tmp_path):
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = _punch(c, token, hisj, "lunch_start")
    assert r.status_code == 409
    assert r.json()["detail"] == "clock in before starting lunch"
    assert db_session.execute(select(Punch)).scalars().all() == []


def test_clock_out_without_clocking_in_is_refused(db_engine, db_session, tmp_path):
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    r = _punch(c, token, hisj, "clock_out")
    assert r.status_code == 409
    assert r.json()["detail"] == "not clocked in"


def test_clock_out_while_on_lunch_is_refused(db_engine, db_session, tmp_path):
    """THE expensive one. An open lunch is never deducted, so a clock_out over
    the top of it pays the break as worked time."""
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    assert _punch(c, token, hisj, "clock_in").status_code == 201
    assert _punch(c, token, hisj, "lunch_start").status_code == 201

    r = _punch(c, token, hisj, "clock_out")
    assert r.status_code == 409
    assert r.json()["detail"] == "end lunch before clocking out"

    # End the lunch and the same clock_out goes through.
    assert _punch(c, token, hisj, "lunch_end").status_code == 201
    assert _punch(c, token, hisj, "clock_out").status_code == 201


def test_double_clock_in_is_refused(db_engine, db_session, tmp_path):
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    assert _punch(c, token, hisj, "clock_in").status_code == 201
    # Far enough apart to clear the debounce, so this is the ORDER rule
    # refusing it and not the double-tap guard.
    punch = db_session.execute(select(Punch)).scalars().one()
    punch.punched_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.commit()

    r = _punch(c, token, hisj, "clock_in")
    assert r.status_code == 409
    assert r.json()["detail"] == "already clocked in"


def test_a_forgotten_clock_out_does_not_block_the_next_day(db_engine, db_session, tmp_path):
    """A stale open shift reads as 'out'. Someone who forgot to clock out on
    Tuesday is not still on shift on Wednesday, and refusing their clock_in
    would turn one missed punch into an unpayable day. The gap still surfaces
    as a `missing_clock_out` warning on the card."""
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    assert _punch(c, token, hisj, "clock_in").status_code == 201
    punch = db_session.execute(select(Punch)).scalars().one()
    punch.punched_at = datetime.now(UTC) - timedelta(hours=20)
    db_session.commit()

    assert _punch(c, token, hisj, "clock_in").status_code == 201


def test_an_overnight_shift_can_still_clock_out(db_engine, db_session, tmp_path):
    """The lookback is generous enough for a real graveyard shift: clocked in
    at 22:00, still clocking out at 06:00 the next morning."""
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    assert _punch(c, token, hisj, "clock_in").status_code == 201
    punch = db_session.execute(select(Punch)).scalars().one()
    punch.punched_at = datetime.now(UTC) - timedelta(hours=8)
    db_session.commit()

    assert _punch(c, token, hisj, "clock_out").status_code == 201


def test_punch_state_reports_what_is_allowed_next(db_engine, db_session, tmp_path):
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    hdr = {"X-Kiosk-Token": token}

    r = c.get(f"/api/kiosk/punch-state?employee_id={hisj}", headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"employee_id": hisj, "state": "out", "allowed": ["clock_in"]}

    _punch(c, token, hisj, "clock_in")
    assert c.get(f"/api/kiosk/punch-state?employee_id={hisj}", headers=hdr).json()["allowed"] == [
        "lunch_start", "clock_out",
    ]

    _punch(c, token, hisj, "lunch_start")
    body = c.get(f"/api/kiosk/punch-state?employee_id={hisj}", headers=hdr).json()
    assert body["state"] == "on_break"
    assert body["allowed"] == ["lunch_end"]


def test_punch_state_is_confined_to_this_kiosks_property(db_engine, db_session, tmp_path):
    """Same 403 as /punch for every denial — otherwise this becomes the
    existence oracle that endpoint was fixed to stop being."""
    token, _d, _h, sssj = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    hdr = {"X-Kiosk-Token": token}
    assert c.get(f"/api/kiosk/punch-state?employee_id={sssj}", headers=hdr).status_code == 403
    assert c.get("/api/kiosk/punch-state?employee_id=999999", headers=hdr).status_code == 403


def test_punch_state_requires_a_device_token(db_engine, db_session, tmp_path):
    _token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    assert c.get(f"/api/kiosk/punch-state?employee_id={hisj}").status_code == 401


def test_roster_says_who_is_on_the_clock(db_engine, db_session, tmp_path):
    """The roster tile is bordered by state, so the state has to come with it —
    one query for the whole roster, not one per tile."""
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    hdr = {"X-Kiosk-Token": token}

    row = c.get("/api/kiosk/employees", headers=hdr).json()[0]
    assert row["state"] == "out"

    _punch(c, token, hisj, "clock_in")
    assert c.get("/api/kiosk/employees", headers=hdr).json()[0]["state"] == "in"

    _punch(c, token, hisj, "lunch_start")
    assert c.get("/api/kiosk/employees", headers=hdr).json()[0]["state"] == "on_break"


def test_search_does_not_disclose_punch_state(db_engine, db_session, tmp_path):
    """The roster shows every active name already, so its state column is one
    more fact about people on the screen. SEARCH answers a name you typed —
    carrying state there would let anyone at the device probe whether one
    specific person is working right now."""
    token, _d, hisj, _s = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path, InMemoryPhotoStore()))
    hdr = {"X-Kiosk-Token": token}
    _punch(c, token, hisj, "clock_in")

    rows = c.get("/api/kiosk/search?q=Hank", headers=hdr).json()
    assert rows and all(set(r) == {"employee_id", "full_name"} for r in rows)
