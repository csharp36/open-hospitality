"""Endpoint tests for the D1 schedule builder: templates + week/shift CRUD.

Times serialize as "HH:MM" in responses (the frontend mirrors this in Task 5).
Every write surface is exercised for role gating (accountant 403, unauth 401)
and property confinement — including the adversarial path where the caller
reaches another property's week or shift by ID rather than by property param.
"""

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql as postgresql_dialect

from tests.employees import make_employee, set_rate, set_rate_everywhere
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    Department,
    Employee,
    EmployeeAssignment,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
    Schedule,
    Shift,
    ShiftTemplate,
)
from usali.photo_store import InMemoryPhotoStore
from usali.schedule_api import _publish_lock_stmt
from usali.server import create_app
from usali.timecards import assemble_timecard
from tests.authkit import make_authkit
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

# A Monday on the payroll grid (anchor 2026-01-05, also a Monday).
_WEEK = "2026-07-06"


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    return TestClient(app)


def _seed(db_session):
    """Two properties. HISJ gets a department, an active and a terminated
    employee; SSSJ gets its own department + employee for cross-property
    refusals."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add_all([
        Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"),
        Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="OPERA", wage_jurisdiction="US-CA"),
    ])
    db_session.flush()
    dept = Department(property_id="HISJ", name="Front Desk")
    foreign_dept = Department(property_id="SSSJ", name="Front Desk")
    db_session.add_all([dept, foreign_dept])
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H", pay_type="hourly")
    gone = make_employee(db_session, property_id="HISJ", full_name="Gone G", pay_type="hourly",
                    termination_date=date(2026, 6, 1))
    faye = make_employee(db_session, property_id="SSSJ", full_name="Faye F", pay_type="hourly")
    db_session.add_all([emp, gone, faye])
    db_session.commit()
    # L4: role authority is DB grants, not token roles — plant each
    # minted subject's grant (scope still comes from the token claim).
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "accountant", sub="acct")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    grant_role(db_session, "property_gm", sub="gm3", property_id="SSSJ")
    return {
        "dept": dept.department_id, "foreign_dept": foreign_dept.department_id,
        "emp": emp.employee_id, "gone": gone.employee_id, "faye": faye.employee_id,
    }


def _seed_costing(db_session, ids):
    """Extend `_seed` with what pricing needs: a second HISJ department, an
    exempt position, and rated colleagues. Hank gets THE distinctive rate
    23.17 — the adversarial tests assert that string appears nowhere in any
    projection response."""
    dept2 = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept2)
    db_session.flush()
    exempt_pos = Position(department_id=ids["dept"], title="Manager", flsa_exempt=True)
    db_session.add(exempt_pos)
    db_session.flush()
    hank = db_session.get(Employee, ids["emp"])
    # Hank was placed by an earlier seed, so his rate goes on that PLACEMENT --
    # `hank.pay_rate = ...` would set an attribute costing no longer reads.
    set_rate_everywhere(db_session, hank, "23.17")
    barb = make_employee(db_session, property_id="HISJ", full_name="Barb B", pay_type="hourly",
                    pay_rate="20.00")
    cara = make_employee(db_session, property_id="HISJ", full_name="Cara C", pay_type="hourly",
                    pay_rate="10.00")
    nora = make_employee(db_session, property_id="HISJ", full_name="Nora N", pay_type="hourly")  # rate-less
    sal = make_employee(db_session, property_id="HISJ", full_name="Sal S", pay_type="salary",
                   position_id=exempt_pos.position_id, pay_rate="50.00")
    db_session.add_all([barb, cara, nora, sal])
    db_session.commit()
    ids.update(dept2=dept2.department_id, barb=barb.employee_id,
               cara=cara.employee_id, nora=nora.employee_id, sal=sal.employee_id)
    return ids


def _admin(mint):
    return {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}


def _gm(mint, prop="HISJ", sub="gm"):
    tok = mint(roles=["property_gm"], sub=sub,
               scopes=[{"property_id": prop, "department_id": None}])
    return {"Authorization": f"Bearer {tok}"}


def _template(ids, **overrides):
    body = {"property": "HISJ", "department_id": ids["dept"],
            "name": "Front Desk AM", "start_time": "07:00", "end_time": "15:00",
            "crosses_midnight": False}
    body.update(overrides)
    return body


def _shift(ids, **overrides):
    body = {"business_date": _WEEK, "department_id": ids["dept"],
            "start_time": "09:00", "end_time": "17:00", "crosses_midnight": False,
            "employee_id": ids["emp"]}
    body.update(overrides)
    return body


def _make_week(c, headers, property_id="HISJ", week_start=_WEEK):
    r = c.post("/api/schedule/weeks", headers=headers,
               json={"property": property_id, "week_start": week_start})
    assert r.status_code == 201, r.text
    return r.json()["schedule_id"]


# --- templates -------------------------------------------------------------


def test_template_round_trip(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    r = c.post("/api/schedule/templates", headers=admin, json=_template(ids))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Front Desk AM"
    assert body["property_id"] == "HISJ"
    # Times serialize as zero-padded "HH:MM" — the frontend contract.
    assert body["start_time"] == "07:00"
    assert body["end_time"] == "15:00"
    assert body["crosses_midnight"] is False
    tpl_id = body["template_id"]

    listed = c.get("/api/schedule/templates", params={"property": "HISJ"},
                   headers=admin)
    assert listed.status_code == 200
    assert [t["template_id"] for t in listed.json()] == [tpl_id]

    assert c.delete(f"/api/schedule/templates/{tpl_id}",
                    headers=admin).status_code == 204
    assert c.get("/api/schedule/templates", params={"property": "HISJ"},
                 headers=admin).json() == []


def test_duplicate_template_name_is_409(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    assert c.post("/api/schedule/templates", headers=admin,
                  json=_template(ids)).status_code == 201
    assert c.post("/api/schedule/templates", headers=admin,
                  json=_template(ids, start_time="08:00")).status_code == 409
    assert len(db_session.execute(select(ShiftTemplate)).scalars().all()) == 1


def test_template_department_must_belong_to_the_property(
    db_engine, db_session, tmp_path
):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    # SSSJ's department on an HISJ template — and an unknown department — are
    # the same refusal (no existence oracle over department ids).
    for bad in (ids["foreign_dept"], 99999):
        r = c.post("/api/schedule/templates", headers=admin,
                   json=_template(ids, department_id=bad))
        assert r.status_code == 422, r.text
    assert db_session.execute(select(ShiftTemplate)).scalars().all() == []


def test_delete_template_referenced_by_a_shift_is_409(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    tpl_id = c.post("/api/schedule/templates", headers=admin,
                    json=_template(ids)).json()["template_id"]
    week_id = _make_week(c, admin)
    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids, template_id=tpl_id))
    assert r.status_code == 201, r.text
    shift_id = r.json()["shift_id"]

    # Provenance: a referenced template cannot vanish from under its shifts.
    assert c.delete(f"/api/schedule/templates/{tpl_id}",
                    headers=admin).status_code == 409
    assert db_session.get(ShiftTemplate, tpl_id) is not None

    # Once the shift is gone the template may go too.
    assert c.delete(f"/api/schedule/shifts/{shift_id}",
                    headers=admin).status_code == 204
    assert c.delete(f"/api/schedule/templates/{tpl_id}",
                    headers=admin).status_code == 204


def test_gm_is_confined_on_templates(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    sssj_gm = _gm(mint, prop="SSSJ")

    assert c.post("/api/schedule/templates", headers=sssj_gm,
                  json=_template(ids)).status_code == 403
    assert c.get("/api/schedule/templates", params={"property": "HISJ"},
                 headers=sssj_gm).status_code == 403

    tpl_id = c.post("/api/schedule/templates", headers=admin,
                    json=_template(ids)).json()["template_id"]
    assert c.delete(f"/api/schedule/templates/{tpl_id}",
                    headers=sssj_gm).status_code == 403
    assert db_session.get(ShiftTemplate, tpl_id) is not None

    # A GM scoped to the property itself is the whole point of the surface.
    hisj_gm = _gm(mint, prop="HISJ")
    assert c.get("/api/schedule/templates", params={"property": "HISJ"},
                 headers=hisj_gm).status_code == 200


def test_accountant_is_403_everywhere(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}

    assert c.post("/api/schedule/templates", headers=acct,
                  json=_template(ids)).status_code == 403
    assert c.get("/api/schedule/templates", params={"property": "HISJ"},
                 headers=acct).status_code == 403
    assert c.post("/api/schedule/weeks", headers=acct,
                  json={"property": "HISJ", "week_start": _WEEK}).status_code == 403
    assert c.get("/api/schedule/weeks",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=acct).status_code == 403
    assert c.post("/api/schedule/weeks/1/shifts", headers=acct,
                  json=_shift(ids)).status_code == 403
    assert c.put("/api/schedule/shifts/1", headers=acct,
                 json=_shift(ids)).status_code == 403
    assert c.delete("/api/schedule/shifts/1", headers=acct).status_code == 403
    assert c.get("/api/schedule/weeks/1/projection", headers=acct).status_code == 403
    assert c.post("/api/schedule/weeks/1/publish", headers=acct).status_code == 403
    assert db_session.execute(select(Schedule)).scalars().all() == []


def test_unauthenticated_is_401(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, _mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)

    assert c.get("/api/schedule/templates",
                 params={"property": "HISJ"}).status_code == 401
    assert c.post("/api/schedule/weeks",
                  json={"property": "HISJ", "week_start": _WEEK}).status_code == 401
    assert c.post("/api/schedule/weeks/1/shifts", json=_shift(ids)).status_code == 401
    assert c.get("/api/schedule/weeks/1/projection").status_code == 401
    assert c.post("/api/schedule/weeks/1/publish").status_code == 401


# --- weeks -----------------------------------------------------------------


def test_create_week_returns_a_draft_at_version_zero(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    r = c.post("/api/schedule/weeks", headers=admin,
               json={"property": "HISJ", "week_start": _WEEK})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["version"] == 0
    assert body["published_at"] is None
    assert body["week_start"] == _WEEK
    assert body["shifts"] == []

    got = c.get("/api/schedule/weeks",
                params={"property": "HISJ", "week_start": _WEEK}, headers=admin)
    assert got.status_code == 200
    assert got.json()["schedule_id"] == body["schedule_id"]


def test_off_grid_week_start_is_422(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    # A Tuesday and a Sunday — any non-Monday is off the payroll grid. With
    # the real anchor being a Monday, %7-alignment already implies Monday, so
    # the explicit weekday() != 0 branch in create_week is belt-and-braces (a
    # Tuesday cannot be %7-aligned under this anchor and fails BOTH checks);
    # both paths raise the same grid 422.
    for bad in ("2026-07-07", "2026-07-12"):
        r = c.post("/api/schedule/weeks", headers=admin,
                   json={"property": "HISJ", "week_start": bad})
        assert r.status_code == 422, bad
        assert "grid" in r.json()["detail"]
    assert db_session.execute(select(Schedule)).scalars().all() == []


def test_duplicate_week_is_409(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    _make_week(c, admin)
    r = c.post("/api/schedule/weeks", headers=admin,
               json={"property": "HISJ", "week_start": _WEEK})
    assert r.status_code == 409
    assert len(db_session.execute(select(Schedule)).scalars().all()) == 1
    # The SAME Monday at the OTHER property is a different week — allowed.
    _make_week(c, admin, property_id="SSSJ")


def test_absent_week_is_404(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/schedule/weeks",
              params={"property": "HISJ", "week_start": _WEEK},
              headers=_admin(mint))
    assert r.status_code == 404


def test_gm_is_confined_on_weeks(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    sssj_gm = _gm(mint, prop="SSSJ")

    assert c.post("/api/schedule/weeks", headers=sssj_gm,
                  json={"property": "HISJ", "week_start": _WEEK}).status_code == 403
    assert db_session.execute(select(Schedule)).scalars().all() == []

    _make_week(c, _admin(mint))
    assert c.get("/api/schedule/weeks",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=sssj_gm).status_code == 403
    assert c.get("/api/schedule/weeks",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=_gm(mint, prop="HISJ")).status_code == 200


# --- shifts ----------------------------------------------------------------


def test_add_assigned_and_open_shifts(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["employee_id"] == ids["emp"]
    assert body["start_time"] == "09:00" and body["end_time"] == "17:00"
    assert body["business_date"] == _WEEK

    # An OPEN shift — coverage nobody is assigned to yet — is a 201, not an error.
    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids, employee_id=None, start_time="15:00",
                           end_time="23:00"))
    assert r.status_code == 201, r.text
    assert r.json()["employee_id"] is None

    week = c.get("/api/schedule/weeks",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=admin).json()
    assert [s["start_time"] for s in week["shifts"]] == ["09:00", "15:00"]


def test_business_date_outside_the_week_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    # The Sunday before and the Monday after the week's 7 dates.
    for bad in ("2026-07-05", "2026-07-13"):
        r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                   json=_shift(ids, business_date=bad))
        assert r.status_code == 422, bad
    # The week's LAST day (Sunday the 12th) is inside.
    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                  json=_shift(ids, business_date="2026-07-12")).status_code == 201


def test_overlapping_shift_for_the_same_employee_is_422(
    db_engine, db_session, tmp_path
):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                  json=_shift(ids)).status_code == 201  # 09:00-17:00
    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids, start_time="16:00", end_time="22:00"))
    assert r.status_code == 422
    assert "overlap" in r.json()["detail"]
    # Back-to-back (17:00 start) is NOT overlap.
    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                  json=_shift(ids, start_time="17:00",
                              end_time="22:00")).status_code == 201
    # Overlap is PER EMPLOYEE: an open shift at the same times is coverage,
    # not a conflict.
    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                  json=_shift(ids, employee_id=None, start_time="16:00",
                              end_time="22:00")).status_code == 201
    assert len(db_session.execute(select(Shift)).scalars().all()) == 3


def test_midnight_crossing_overlap_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    # Night audit Mon 23:00 -> Tue 07:00.
    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                  json=_shift(ids, start_time="23:00", end_time="07:00",
                              crosses_midnight=True)).status_code == 201
    # Tue 06:00-14:00 starts before the night shift ends — overlap.
    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids, business_date="2026-07-07", start_time="06:00",
                           end_time="14:00"))
    assert r.status_code == 422
    # Tue 07:00-15:00 is back-to-back with the night shift's end — allowed.
    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                  json=_shift(ids, business_date="2026-07-07", start_time="07:00",
                              end_time="15:00")).status_code == 201


def test_cross_week_overlap_with_the_adjacent_week_is_422(
    db_engine, db_session, tmp_path
):
    """A crosses-midnight Sunday shift in week N spills into week N+1's Monday
    morning — the overlap 422 must see it across the schedule boundary, not
    only within one week."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_n = _make_week(c, admin)  # 2026-07-06
    week_n1 = _make_week(c, admin, week_start="2026-07-13")

    # Week N's Sunday 2026-07-12, 23:00 -> Monday 07:00 (crosses midnight).
    assert c.post(f"/api/schedule/weeks/{week_n}/shifts", headers=admin,
                  json=_shift(ids, business_date="2026-07-12",
                              start_time="23:00", end_time="07:00",
                              crosses_midnight=True)).status_code == 201
    # Week N+1's Monday 06:00 starts before the night shift ends — overlap,
    # even though the two shifts live in different schedules.
    r = c.post(f"/api/schedule/weeks/{week_n1}/shifts", headers=admin,
               json=_shift(ids, business_date="2026-07-13",
                           start_time="06:00", end_time="14:00"))
    assert r.status_code == 422
    assert "overlap" in r.json()["detail"]
    # Monday 08:00 clears the night shift's 07:00 end — allowed.
    assert c.post(f"/api/schedule/weeks/{week_n1}/shifts", headers=admin,
                  json=_shift(ids, business_date="2026-07-13",
                              start_time="08:00", end_time="16:00")).status_code == 201


def test_terminated_employee_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids, employee_id=ids["gone"]))
    assert r.status_code == 422
    assert "terminated" in r.json()["detail"]
    assert db_session.execute(select(Shift)).scalars().all() == []


def test_employee_from_another_property_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    # SSSJ's employee — and an unknown id — are the same refusal (no existence
    # oracle over another property's employee ids).
    for bad in (ids["faye"], 99999):
        r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                   json=_shift(ids, employee_id=bad))
        assert r.status_code == 422, r.text
        assert "Faye" not in r.text and "SSSJ" not in r.text
    assert db_session.execute(select(Shift)).scalars().all() == []


def test_department_from_another_property_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids, department_id=ids["foreign_dept"]))
    assert r.status_code == 422
    assert db_session.execute(select(Shift)).scalars().all() == []


def test_template_from_another_property_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    sssj_tpl = c.post("/api/schedule/templates", headers=admin,
                      json=_template(ids, property="SSSJ",
                                     department_id=ids["foreign_dept"]))
    assert sssj_tpl.status_code == 201
    week_id = _make_week(c, admin)

    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
               json=_shift(ids, template_id=sssj_tpl.json()["template_id"]))
    assert r.status_code == 422
    assert db_session.execute(select(Shift)).scalars().all() == []


def test_end_before_start_without_crossing_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    # A backwards span and a zero-length span are not shifts; a real overnight
    # shift must say crosses_midnight.
    for bad in ({"start_time": "17:00", "end_time": "09:00"},
                {"start_time": "09:00", "end_time": "09:00"}):
        r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                   json=_shift(ids, **bad))
        assert r.status_code == 422, r.text
    assert db_session.execute(select(Shift)).scalars().all() == []


def test_put_edits_and_revalidates(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    shift_id = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                      json=_shift(ids)).json()["shift_id"]

    # Re-saving the same times is NOT a self-overlap.
    r = c.put(f"/api/schedule/shifts/{shift_id}", headers=admin, json=_shift(ids))
    assert r.status_code == 200, r.text

    # A PUT cannot drift the date outside the week...
    assert c.put(f"/api/schedule/shifts/{shift_id}", headers=admin,
                 json=_shift(ids, business_date="2026-07-13")).status_code == 422
    # ...move the shift to another property's employee...
    assert c.put(f"/api/schedule/shifts/{shift_id}", headers=admin,
                 json=_shift(ids, employee_id=ids["faye"])).status_code == 422
    # ...or hand it to a terminated one.
    assert c.put(f"/api/schedule/shifts/{shift_id}", headers=admin,
                 json=_shift(ids, employee_id=ids["gone"])).status_code == 422

    # Un-assigning to an open shift is a legitimate edit.
    r = c.put(f"/api/schedule/shifts/{shift_id}", headers=admin,
              json=_shift(ids, employee_id=None))
    assert r.status_code == 200
    assert r.json()["employee_id"] is None
    shift = db_session.get(Shift, shift_id)
    assert shift is not None and shift.employee_id is None


def test_put_overlap_with_another_shift_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                  json=_shift(ids, start_time="09:00",
                              end_time="13:00")).status_code == 201
    second = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                    json=_shift(ids, start_time="14:00",
                                end_time="18:00")).json()["shift_id"]
    r = c.put(f"/api/schedule/shifts/{second}", headers=admin,
              json=_shift(ids, start_time="12:00", end_time="18:00"))
    assert r.status_code == 422
    shift = db_session.get(Shift, second)
    assert shift is not None and shift.start_time.strftime("%H:%M") == "14:00"


def test_delete_shift(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    shift_id = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                      json=_shift(ids)).json()["shift_id"]

    assert c.delete(f"/api/schedule/shifts/{shift_id}",
                    headers=admin).status_code == 204
    assert db_session.execute(select(Shift)).scalars().all() == []
    assert c.delete(f"/api/schedule/shifts/{shift_id}",
                    headers=admin).status_code == 404


def test_unknown_schedule_or_shift_is_404(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    assert c.post("/api/schedule/weeks/99999/shifts", headers=admin,
                  json=_shift(ids)).status_code == 404
    assert c.put("/api/schedule/shifts/99999", headers=admin,
                 json=_shift(ids)).status_code == 404
    assert c.delete("/api/schedule/shifts/99999", headers=admin).status_code == 404


def test_shift_routes_confine_on_the_schedules_property(
    db_engine, db_session, tmp_path
):
    """THE adversarial path: a GM scoped to SSSJ reaches an HISJ week or shift
    by ID. Confinement runs on the SCHEDULE'S property — the id the caller
    picked asserts nothing — so every touch is a 403 and nothing changes."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    sssj_gm = _gm(mint, prop="SSSJ")
    week_id = _make_week(c, admin)
    shift_id = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=admin,
                      json=_shift(ids)).json()["shift_id"]

    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=sssj_gm,
               json=_shift(ids, start_time="18:00", end_time="22:00"))
    assert r.status_code == 403
    assert "HISJ" not in r.text and "Hank" not in r.text
    assert c.put(f"/api/schedule/shifts/{shift_id}", headers=sssj_gm,
                 json=_shift(ids, employee_id=None)).status_code == 403
    assert c.delete(f"/api/schedule/shifts/{shift_id}",
                    headers=sssj_gm).status_code == 403

    shifts = db_session.execute(select(Shift)).scalars().all()
    assert len(shifts) == 1 and shifts[0].employee_id == ids["emp"]

    # The co-held global VIEW role does not widen it (the A2.3 rule).
    both = {"Authorization": f"Bearer {mint(roles=['accountant', 'property_gm'], sub='gm3', scopes=[{'property_id': 'SSSJ', 'department_id': None}])}"}
    assert c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=both,
                  json=_shift(ids, start_time="18:00",
                              end_time="22:00")).status_code == 403

    # The HISJ GM, of course, can.
    assert c.post(f"/api/schedule/weeks/{week_id}/shifts",
                  headers=_gm(mint, prop="HISJ"),
                  json=_shift(ids, start_time="18:00",
                              end_time="22:00")).status_code == 201


# --- projection ------------------------------------------------------------


def _add(c, headers, week_id, body):
    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["shift_id"]


def _projection(c, headers, week_id):
    r = c.get(f"/api/schedule/weeks/{week_id}/projection", headers=headers)
    assert r.status_code == 200, r.text
    return r


def test_projection_prices_department_ot_at_the_multipliers(
    db_engine, db_session, tmp_path
):
    """Hank (23.17): one 10h day -> 8h reg + 2h OT. Barb (20.00): one 6h day.
    Department cost = 8x23.17 + 2x(23.17x1.5) + 6x20.00 = 374.87 — reg at 1x,
    OT at 1.5x, from each employee's own day-level split."""
    ids = _seed_costing(db_session, _seed(db_session))
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    # 6:00-16:30 spans 10.5h -> 10h after the assumed meal -> 8 reg + 2 OT.
    _add(c, admin, week_id, _shift(ids, start_time="06:00", end_time="16:30"))
    # 10:00-16:00 is exactly 6h -> no meal deduction.
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="10:00", end_time="16:00"))

    body = _projection(c, admin, week_id).json()
    by_emp = {e["employee_id"]: e for e in body["employees"]}
    assert by_emp[ids["emp"]] == {
        "employee_id": ids["emp"], "full_name": "Hank H",
        "total_hours": "10.00", "regular_hours": "8.00", "ot_hours": "2.00",
    }
    assert by_emp[ids["barb"]]["total_hours"] == "6.00"
    [w] = body["warnings"]
    assert w == {"code": "scheduled_overtime", "employee_id": ids["emp"],
                 "full_name": "Hank H", "business_date": _WEEK, "hours": "2.00"}
    [dept] = body["departments"]
    assert dept == {"department": "Front Desk", "hours": "16.00",
                    "est_cost": "374.87"}
    assert body["total_est_cost"] == "374.87"
    assert body["suppressed_departments"] == 0
    assert body["unpriced_hours"] == "0.00"


def test_projection_never_leaks_a_rate_or_per_employee_cost(
    db_engine, db_session, tmp_path
):
    """THE adversarial response-text assertion. Hank carries the distinctive
    rate 23.17; his 6h day would cost 139.02, Barb's 120.00, Cara's 60.00.
    NONE of those strings — nor any rate — may appear anywhere in the response
    text: only department aggregates (and null for the suppressed department,
    which here is 2 ASSIGNED / 1 PRICED — Cara plus rate-less Nora — the exact
    shape an assigned-population counter would leak)."""
    ids = _seed_costing(db_session, _seed(db_session))
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    _add(c, admin, week_id, _shift(ids, start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="10:00", end_time="16:00"))
    # Cara + rate-less Nora in Housekeeping: 2 assigned, 1 priced — the
    # aggregate would BE Cara's solo 60.00, so it suppresses exactly like the
    # solo department it economically is.
    _add(c, admin, week_id, _shift(ids, employee_id=ids["cara"],
                                   department_id=ids["dept2"],
                                   business_date="2026-07-07",
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["nora"],
                                   department_id=ids["dept2"],
                                   business_date="2026-07-08",
                                   start_time="10:00", end_time="16:00"))

    r = _projection(c, admin, week_id)
    # No rate, anywhere, ever.
    assert "23.17" not in r.text
    assert "20.00" not in r.text and "10.00" not in r.text
    # No per-employee cost value either (rate = cost / hours re-derivation).
    for per_employee_cost in ("139.02", "120.00", "60.00"):
        assert per_employee_cost not in r.text
    # And no naive total that would include the suppressed department.
    assert "319.02" not in r.text

    body = r.json()
    depts = {d["department"]: d for d in body["departments"]}
    # The two-PRICED department shows its aggregate: 139.02 + 120.00.
    assert depts["Front Desk"] == {"department": "Front Desk", "hours": "12.00",
                                   "est_cost": "259.02"}
    # The one-priced department: hours show, est_cost is NULL, and it is
    # EXCLUDED from the total (complementary suppression — the C3 lesson).
    assert depts["Housekeeping"]["hours"] == "12.00"
    assert depts["Housekeeping"]["est_cost"] is None
    assert body["total_est_cost"] == "259.02"
    assert body["suppressed_departments"] == 1
    assert body["unpriced_hours"] == "6.00"  # Nora's


def test_projection_splits_a_multi_department_day_proportionally(
    db_engine, db_session, tmp_path
):
    """Barb's Monday spans two departments (6h Front Desk + 6h Housekeeping =
    12h -> 8 reg + 4 OT, day cost 280.00). The day's cost is attributed by each
    department's SHARE of the day's hours (140.00 each), never dumped on one.
    Cara PRICES both departments (Front Desk Monday, Housekeeping Tuesday) so
    both clear the two-priced-employee floor and the split shows UNSUPPRESSED:
    a dump-on-one-department bug would read 340.00 / 60.00, not 200.00 / 200.00."""
    ids = _seed_costing(db_session, _seed(db_session))
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    # Barb (20.00): back-to-back Front Desk 6:00-12:00 + Housekeeping
    # 12:00-18:30 (6h after the meal). Day = 12h -> 8 reg + 4 OT.
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="06:00", end_time="12:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   department_id=ids["dept2"],
                                   start_time="12:00", end_time="18:30"))
    # Cara (10.00) is the second PRICED contributor in EACH department — the
    # suppression floor counts priced employees, so a rate-less colleague
    # would not keep these aggregates visible.
    _add(c, admin, week_id, _shift(ids, employee_id=ids["cara"],
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["cara"],
                                   department_id=ids["dept2"],
                                   business_date="2026-07-07",
                                   start_time="10:00", end_time="16:00"))

    r = _projection(c, admin, week_id)
    body = r.json()
    depts = {d["department"]: d for d in body["departments"]}
    # Front Desk: Barb's 6/12 share of 280.00 (=140.00) + Cara's Monday 60.00.
    assert depts["Front Desk"] == {"department": "Front Desk", "hours": "12.00",
                                   "est_cost": "200.00"}
    # Housekeeping: Barb's other 140.00 + Cara's Tuesday 60.00.
    assert depts["Housekeeping"] == {"department": "Housekeeping",
                                     "hours": "12.00", "est_cost": "200.00"}
    assert body["total_est_cost"] == "400.00"
    assert body["suppressed_departments"] == 0
    assert body["unpriced_hours"] == "0.00"
    [w] = body["warnings"]
    assert w["code"] == "scheduled_overtime" and w["hours"] == "4.00"
    assert w["employee_id"] == ids["barb"]
    # Barb's whole-day cost must not appear — it belongs to no one department —
    # and neither may either bare 140.00 share or Cara's 60.00 day cost.
    assert "280.00" not in r.text and "140.00" not in r.text
    # And still: no rates, no per-employee costs.
    assert "20.00" not in r.text and "10.00" not in r.text
    assert "60.00" not in r.text and "23.17" not in r.text


def test_projection_suppresses_a_multi_department_day_solo_priced_share(
    db_engine, db_session, tmp_path
):
    """The solo-priced flip side of the proportional test: Housekeeping's only
    PRICED contributor is Barb (rate-less Nora adds hours, never a price), so
    its aggregate would BE Barb's 140.00 share — it suppresses even though two
    employees are assigned there."""
    ids = _seed_costing(db_session, _seed(db_session))
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="06:00", end_time="12:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   department_id=ids["dept2"],
                                   start_time="12:00", end_time="18:30"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["cara"],
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["nora"],
                                   department_id=ids["dept2"],
                                   start_time="10:00", end_time="16:00"))

    r = _projection(c, admin, week_id)
    body = r.json()
    depts = {d["department"]: d for d in body["departments"]}
    # Front Desk still prices (Barb + Cara are both priced there).
    assert depts["Front Desk"] == {"department": "Front Desk", "hours": "12.00",
                                   "est_cost": "200.00"}
    # Housekeeping: hours show, money hides — Barb's solo 140.00 share must
    # not surface, alone or inside a total.
    assert depts["Housekeeping"] == {"department": "Housekeeping",
                                     "hours": "12.00", "est_cost": None}
    assert body["total_est_cost"] == "200.00"
    assert body["suppressed_departments"] == 1
    assert body["unpriced_hours"] == "6.00"  # Nora's
    assert "140.00" not in r.text and "280.00" not in r.text
    assert "340.00" not in r.text  # no naive total including the hidden share


def test_projection_two_assigned_one_priced_department_is_suppressed(
    db_engine, db_session, tmp_path
):
    """THE C3-class regression: suppression must count the PRICED population.
    Rara (distinctive rate 19.83) + rate-less Nora staff Front Desk: 2 assigned
    but 1 priced. An assigned-population counter would show est_cost 118.98 —
    which IS Rara's cost, with her 6.00h right in the employees table
    (rate = 118.98 / 6 = 19.83). est_cost must be null: the division has no
    numerator, so nothing is derivable."""
    ids = _seed_costing(db_session, _seed(db_session))
    rara = make_employee(db_session, property_id="HISJ", full_name="Rara R", pay_type="hourly",
                    pay_rate="19.83")
    db_session.add(rara)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    _add(c, admin, week_id, _shift(ids, employee_id=rara.employee_id,
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["nora"],
                                   start_time="09:00", end_time="15:00"))

    r = _projection(c, admin, week_id)
    body = r.json()
    [dept] = body["departments"]
    # Hours are operational and still show; the money cell is null.
    assert dept["hours"] == "12.00"
    assert dept["est_cost"] is None
    assert body["suppressed_departments"] == 1
    assert body["total_est_cost"] == "0.00"
    assert body["unpriced_hours"] == "6.00"  # Nora's
    # Neither the distinctive rate nor the would-be solo cost exists anywhere.
    assert "19.83" not in r.text and "118.98" not in r.text


def test_projection_open_shift_adds_department_hours_but_never_cost(
    db_engine, db_session, tmp_path
):
    ids = _seed_costing(db_session, _seed(db_session))
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["cara"],
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=None,
                                   start_time="09:00", end_time="15:00"))

    body = _projection(c, admin, week_id).json()
    [dept] = body["departments"]
    # 3 x 6h of coverage; cost prices only the two ASSIGNED employees.
    assert dept["hours"] == "18.00"
    assert dept["est_cost"] == "180.00"  # 6x20 + 6x10
    assert body["total_est_cost"] == "180.00"
    # An open shift is unassigned coverage, not an unpriced employee.
    assert body["unpriced_hours"] == "0.00"
    assert [e["employee_id"] for e in body["employees"]] == sorted(
        [ids["barb"], ids["cara"]]
    )


def test_projection_exempt_employee_has_hours_but_no_cost_and_no_ot_warning(
    db_engine, db_session, tmp_path
):
    ids = _seed_costing(db_session, _seed(db_session))
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    # Sal (exempt, salaried): a 10h day. Barb (20.00): a 6h day.
    _add(c, admin, week_id, _shift(ids, employee_id=ids["sal"],
                                   start_time="06:00", end_time="16:30"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="10:00", end_time="16:00"))

    r = _projection(c, admin, week_id)
    body = r.json()
    by_emp = {e["employee_id"]: e for e in body["employees"]}
    # Hours show — all regular (the engine never flags an exempt day as OT)...
    assert by_emp[ids["sal"]]["total_hours"] == "10.00"
    assert by_emp[ids["sal"]]["ot_hours"] == "0.00"
    assert body["warnings"] == []
    # ...but the hardened B3 rule holds TWICE over: salaried staff are never
    # hourly-costed, AND a department whose cost would come from Barb ALONE is
    # suppressed — 2 ASSIGNED but 1 PRICED means the aggregate IS Barb's cost,
    # and her 6.00h sit in the table above (rate = cost / hours). Suppression
    # counts the PRICED population, not the assigned one.
    [dept] = body["departments"]
    assert dept["hours"] == "16.00"
    assert dept["est_cost"] is None
    assert body["suppressed_departments"] == 1
    assert body["total_est_cost"] == "0.00"
    assert body["unpriced_hours"] == "10.00"
    # Sal's on-file rate must never surface, hourly-costed or otherwise —
    # and neither may Barb's solo cost (120.00) or rate (20.00).
    assert "50.00" not in r.text and "500.00" not in r.text
    assert "120.00" not in r.text and "20.00" not in r.text


def test_projection_of_an_empty_week(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    body = _projection(c, admin, week_id).json()
    assert body["employees"] == [] and body["warnings"] == []
    assert body["departments"] == []
    assert body["total_est_cost"] == "0.00"
    assert body["suppressed_departments"] == 0
    assert body["unpriced_hours"] == "0.00"


def test_projection_and_publish_confine_on_the_schedules_property(
    db_engine, db_session, tmp_path
):
    ids = _seed_costing(db_session, _seed(db_session))
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    sssj_gm = _gm(mint, prop="SSSJ")
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))

    r = c.get(f"/api/schedule/weeks/{week_id}/projection", headers=sssj_gm)
    assert r.status_code == 403
    assert "23.17" not in r.text and "Hank" not in r.text and "HISJ" not in r.text

    assert c.post(f"/api/schedule/weeks/{week_id}/publish",
                  headers=sssj_gm).status_code == 403
    sched = db_session.get(Schedule, week_id)
    assert sched is not None
    assert sched.status == "draft" and sched.version == 0
    assert db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "publish_schedule")
    ).scalars().all() == []

    # The property's own GM sees the projection, of course.
    assert c.get(f"/api/schedule/weeks/{week_id}/projection",
                 headers=_gm(mint, prop="HISJ")).status_code == 200


# --- projection: current-week merge + adjacent-week context (D3) -----------
#
# Tests ALWAYS pass `as_of` explicitly: the server-today default would make
# these fixed 2026-07 weeks date-dependent. The dynamic-future test below is
# the one place the default is exercised — against a week guaranteed future.

_ANCHOR = date(2026, 1, 5)  # the payroll grid anchor (a Monday)


def _kiosk(db_session):
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="adm")
    db_session.add(device)
    db_session.flush()
    return device.device_id


def _punch_day(db_session, device_id, emp_id, day, start_h=9, end_h=17):
    """A real punched day: clock in/out with a 30-minute lunch (the B1/B2
    shapes, mirroring tests/test_labor_promote.py)."""
    def _p(ptype, hh, mm=0):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC),
            business_date=day,
        ))
    _p("clock_in", start_h)
    _p("lunch_start", 12)
    _p("lunch_end", 12, 30)
    _p("clock_out", end_h)


def _assemble(db_session, emp_id):
    # Assembly links the punches to the (open) card — approval NOT needed:
    # compute_timecard reads the merged timeline of any card.
    assemble_timecard(db_session, emp_id, date(2026, 7, 6), anchor=_ANCHOR)
    db_session.commit()


def _projection_as_of(c, headers, week_id, as_of):
    r = c.get(f"/api/schedule/weeks/{week_id}/projection",
              params={"as_of": as_of}, headers=headers)
    assert r.status_code == 200, r.text
    return r


def test_projection_merges_punched_days_before_as_of(
    db_engine, db_session, tmp_path
):
    """Wednesday's projection of the current week: Monday/Tuesday use PUNCHED
    hours, Wednesday-Friday stay scheduled — and the daily-OT math follows the
    merged reality (Tuesday's long punched day projects 1.50 OT the schedule
    never contained)."""
    ids = _seed(db_session)
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    # Hank scheduled Mon-Fri 09:00-17:00 -> 7.50 meal-adjusted per day.
    for i in range(5):
        d = (date(2026, 7, 6) + timedelta(days=i)).isoformat()
        _add(c, admin, week_id, _shift(ids, business_date=d))
    # Punched reality: Monday as scheduled (7.50), Tuesday long 8-18 (9.50).
    _punch_day(db_session, device_id, ids["emp"], date(2026, 7, 6))
    _punch_day(db_session, device_id, ids["emp"], date(2026, 7, 7),
               start_h=8, end_h=18)
    _assemble(db_session, ids["emp"])

    body = _projection_as_of(c, admin, week_id, "2026-07-08").json()
    assert body["merged_through"] == "2026-07-07"
    [emp] = body["employees"]
    # Mon 7.50 punched + Tue 9.50 punched (8 reg + 1.50 daily OT) + Wed-Fri
    # 3 x 7.50 scheduled = 39.50.
    assert emp["total_hours"] == "39.50"
    assert emp["regular_hours"] == "38.00"
    assert emp["ot_hours"] == "1.50"
    [w] = body["warnings"]
    assert w == {"code": "scheduled_overtime", "employee_id": ids["emp"],
                 "full_name": "Hank H", "business_date": "2026-07-07",
                 "hours": "1.50"}
    # department_hours stay schedule-derived — they describe the plan.
    [dept] = body["departments"]
    assert dept["hours"] == "37.50"


def test_projection_zero_punch_elapsed_day_keeps_scheduled_hours(
    db_engine, db_session, tmp_path
):
    """THE ZERO-PUNCH RULE, pinned: merge active (merged_through set) but the
    elapsed Monday has no punches — its SCHEDULED hours stand. Mid-week, no
    punches means "no data yet", not "worked zero"; the adherence endpoint
    owns no-show truth."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _shift(ids))  # Monday 09:00-17:00 -> 7.50

    body = _projection_as_of(c, admin, week_id, "2026-07-08").json()
    assert body["merged_through"] == "2026-07-07"
    [emp] = body["employees"]
    assert emp["total_hours"] == "7.50"  # scheduled retained, NOT zeroed


def test_projection_without_as_of_on_a_future_week_does_not_merge(
    db_engine, db_session, tmp_path
):
    """The server-today default against a guaranteed-future week: no merge,
    merged_through null, hours purely scheduled (the D1 behavior)."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    # Next on-grid Monday at least two weeks out — future whenever the suite
    # runs, so server-today can never fall inside it.
    weeks_out = (date.today() - _ANCHOR).days // 7 + 3
    future = _ANCHOR + timedelta(days=weeks_out * 7)
    week_id = _make_week(c, admin, week_start=future.isoformat())
    _add(c, admin, week_id, _shift(ids, business_date=future.isoformat()))

    body = _projection(c, admin, week_id).json()
    assert body["merged_through"] is None
    [emp] = body["employees"]
    assert emp["total_hours"] == "7.50"


def test_projection_sees_adjacent_week_clopening(db_engine, db_session, tmp_path):
    """Prior-week Sunday 15:00-23:00 then this week's Monday 07:00 — 8h rest
    under the 10h floor across the week boundary. The warning appears in THIS
    week's projection (the pair's second shift is in-week); the prior week's
    projection stays clean — the mirror-image warning is not its to raise."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    prior_id = _make_week(c, admin, week_start="2026-06-29")
    week_id = _make_week(c, admin)  # 2026-07-06
    _add(c, admin, prior_id, _shift(ids, business_date="2026-07-05",
                                    start_time="15:00", end_time="23:00"))
    _add(c, admin, week_id, _shift(ids, business_date="2026-07-06",
                                   start_time="07:00", end_time="15:00"))

    body = _projection_as_of(c, admin, week_id, "2026-07-06").json()
    # as_of == week_start: zero days have elapsed, so nothing merges.
    assert body["merged_through"] is None
    [w] = body["warnings"]
    assert w["code"] == "clopening" and w["business_date"] == "2026-07-06"
    assert w["employee_id"] == ids["emp"]

    prior = _projection_as_of(c, admin, prior_id, "2026-06-29").json()
    assert prior["warnings"] == []


def test_projection_merge_never_leaks_a_rate_or_per_employee_cost(
    db_engine, db_session, tmp_path
):
    """THE MONEY REGRESSION with merge active. Same shape as the no-merge leak
    test, but Hank PUNCHED 7.50 on Monday (scheduled 6.00). Cost is priced
    from the SCHEDULE only (merged pricing would be an as_of differencing
    oracle — see test_projection_cost_is_stable_across_as_of), so est_cost
    stays the plan-priced value. Neither his rate nor any per-employee cost
    (merged or scheduled) may appear; suppression semantics hold identically."""
    ids = _seed_costing(db_session, _seed(db_session))
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    _add(c, admin, week_id, _shift(ids, start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["cara"],
                                   department_id=ids["dept2"],
                                   business_date="2026-07-07",
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["nora"],
                                   department_id=ids["dept2"],
                                   business_date="2026-07-08",
                                   start_time="10:00", end_time="16:00"))
    _punch_day(db_session, device_id, ids["emp"], date(2026, 7, 6))  # 7.50
    _assemble(db_session, ids["emp"])

    r = _projection_as_of(c, admin, week_id, "2026-07-08")
    # No rate, anywhere, ever — merge active changes nothing about the rule.
    assert "23.17" not in r.text
    assert "20.00" not in r.text and "10.00" not in r.text
    # No per-employee cost: not Hank's MERGED day cost (173.78), not his
    # scheduled-day cost (139.02), not Barb's (120.00) or Cara's (60.00).
    for per_employee_cost in ("173.78", "139.02", "120.00", "60.00"):
        assert per_employee_cost not in r.text

    body = r.json()
    assert body["merged_through"] == "2026-07-07"
    by_emp = {e["employee_id"]: e for e in body["employees"]}
    assert by_emp[ids["emp"]]["total_hours"] == "7.50"   # punched Monday
    # Barb and Cara punched nothing on their elapsed days — scheduled stands.
    assert by_emp[ids["barb"]]["total_hours"] == "6.00"
    assert by_emp[ids["cara"]]["total_hours"] == "6.00"
    depts = {d["department"]: d for d in body["departments"]}
    # Front Desk (2 priced) shows the SCHEDULE-priced aggregate:
    # 6x23.17 + 6x20 = 259.02 — Hank's punched 7.50 does NOT reprice it.
    assert depts["Front Desk"]["est_cost"] == "259.02"
    assert depts["Front Desk"]["hours"] == "12.00"  # plan-derived
    # Housekeeping: still 1 priced (Cara + rate-less Nora) -> suppressed,
    # excluded from the total (complementary suppression, unchanged).
    assert depts["Housekeeping"]["est_cost"] is None
    assert body["total_est_cost"] == "259.02"
    assert body["suppressed_departments"] == 1
    assert body["unpriced_hours"] == "6.00"  # Nora's


def test_projection_cost_is_stable_across_as_of(db_engine, db_session, tmp_path):
    """THE DIFFERENCING-ORACLE REGRESSION. est_cost must be a function of the
    SCHEDULE alone — never of `as_of`. When merged (punched) hours were priced,
    two requests at adjacent `as_of` values isolated ONE employee's single day:
    rate = Δest_cost / Δhours, exact — the static >= 2-priced suppression floor
    cannot defend a marginal query. Setup: Hank punched 7.50 on Monday against
    a 6.00 schedule in a 2-priced department. Every department's est_cost and
    total_est_cost must be BYTE-IDENTICAL across Wed / Thu / no-merge requests,
    while the per-employee HOURS still visibly merge (the merge is not broken,
    only severed from money)."""
    ids = _seed_costing(db_session, _seed(db_session))
    device_id = _kiosk(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    # Front Desk, 2 priced: Hank (23.17) + Barb (20.00), Monday 6.00 each.
    _add(c, admin, week_id, _shift(ids, start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=ids["barb"],
                                   start_time="10:00", end_time="16:00"))
    # Hank's punched Monday (7.50) differs from his schedule (6.00) — the
    # exact marginal the oracle would isolate.
    _punch_day(db_session, device_id, ids["emp"], date(2026, 7, 6))
    _assemble(db_session, ids["emp"])

    bodies = {
        label: _projection_as_of(c, admin, week_id, as_of).json()
        for label, as_of in [("wed", "2026-07-08"), ("thu", "2026-07-09"),
                             ("future", "2026-07-20")]
    }

    # The merge still works: merged requests show Hank's punched hours...
    for label in ("wed", "thu"):
        assert bodies[label]["merged_through"] is not None
        hank = next(e for e in bodies[label]["employees"]
                    if e["employee_id"] == ids["emp"])
        assert hank["total_hours"] == "7.50"
    # ...while the no-merge request is the pure plan.
    assert bodies["future"]["merged_through"] is None
    hank = next(e for e in bodies["future"]["employees"]
                if e["employee_id"] == ids["emp"])
    assert hank["total_hours"] == "6.00"

    # But money NEVER moves with as_of: byte-identical across all three.
    baseline = [(d["department"], d["est_cost"])
                for d in bodies["future"]["departments"]]
    for label in ("wed", "thu"):
        assert [(d["department"], d["est_cost"])
                for d in bodies[label]["departments"]] == baseline
        assert bodies[label]["total_est_cost"] == bodies["future"]["total_est_cost"]


# --- publish ---------------------------------------------------------------


def test_publish_bumps_version_stamps_and_audits(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    shift_id = _add(c, admin, week_id, _shift(ids))

    r = c.post(f"/api/schedule/weeks/{week_id}/publish", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert r.json()["version"] == 1  # first publish: 0 -> 1
    assert r.json()["published_at"] is not None  # the UI's "vN published <date>"

    sched = db_session.get(Schedule, week_id)
    assert sched is not None
    assert sched.published_by == "adm" and sched.published_at is not None
    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "publish_schedule")
    ).scalars().all()
    assert [a.resource_id for a in audits] == [str(week_id)]
    assert audits[0].actor_subject == "adm" and audits[0].resource_type == "schedule"

    # Republish, not hard-lock: editing after publish is allowed (goes stale)…
    assert c.put(f"/api/schedule/shifts/{shift_id}", headers=admin,
                 json=_shift(ids, start_time="10:00",
                             end_time="18:00")).status_code == 200
    # …and publishing again bumps the version and writes a SECOND audit row.
    r = c.post(f"/api/schedule/weeks/{week_id}/publish", headers=admin)
    assert r.status_code == 200
    assert r.json()["version"] == 2

    db_session.expire_all()
    sched = db_session.get(Schedule, week_id)
    assert sched is not None and sched.version == 2
    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "publish_schedule")
    ).scalars().all()
    assert len(audits) == 2
    assert {a.resource_id for a in audits} == {str(week_id)}


def test_publish_path_row_locks_the_schedule():
    """Racing publishes must serialize: publish is a read-modify-write
    (version += 1) plus an audit row, and two concurrent publishes without a
    lock would lost-update one version bump while writing two audit rows. A
    true race is not reachable from a unit test, so this asserts the
    MECHANISM: the statement the publish path re-selects the schedule with
    compiles to SELECT ... FOR UPDATE on PostgreSQL."""
    sql = str(_publish_lock_stmt(1).compile(dialect=postgresql_dialect.dialect()))
    assert "FOR UPDATE" in sql


def test_publish_unknown_week_is_404(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    assert c.post("/api/schedule/weeks/99999/publish",
                  headers=_admin(mint)).status_code == 404


def test_projection_a_rate_not_in_effect_that_week_is_not_priced(
    db_engine, db_session, tmp_path
):
    """GATE 4, re-verified for E2 (Task 6). The projection's priced population is
    rate-derived, and E2 gave "unpriced" two new causes that could not exist
    before: a rate row that STARTS after the week, and one that ENDED before it.
    A gate that handles "no rate row at all" does not thereby handle these — the
    D1 Critical was exactly a gate counting the wrong population, and the plan is
    explicit that a fix applied to one gate has repeatedly not reached the rest.

    Vera's rate starts the Monday AFTER this week, so she works priced-at-nothing
    beside Rara. Two assigned, one priced: est_cost must be null, or the figure
    IS Rara's pay with her hours on the adjacent line (118.98 / 6 = 19.83).
    """
    ids = _seed_costing(db_session, _seed(db_session))
    rara = make_employee(db_session, property_id="HISJ", full_name="Rara R",
                         pay_type="hourly", pay_rate="19.83")
    vera = make_employee(db_session, property_id="HISJ", full_name="Vera V",
                         pay_type="hourly")
    db_session.flush()
    vera_assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == vera.employee_id
        )
    ).scalar_one()
    set_rate(db_session, vera_assignment, "41.00",
             effective_from=date(2026, 7, 13))  # the Monday AFTER _WEEK
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)

    _add(c, admin, week_id, _shift(ids, employee_id=rara.employee_id,
                                   start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _shift(ids, employee_id=vera.employee_id,
                                   start_time="09:00", end_time="15:00"))

    r = _projection(c, admin, week_id)
    body = r.json()
    front = next(d for d in body["departments"] if d["department"] == "Front Desk")
    assert front["est_cost"] is None, (
        "one priced employee that week: the cost IS Rara's pay"
    )
    assert "118.98" not in r.text, "Rara's cost must not appear anywhere"
    assert "19.83" not in r.text, "nor her rate"
