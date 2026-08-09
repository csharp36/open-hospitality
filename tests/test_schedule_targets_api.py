"""Endpoint tests for D2's targets prerequisites: labor standards, the GM
occupancy forecast (with history hints from promoted ROOMS_OCCUPIED facts),
and availability notes.

All three surfaces are scheduler-gated (accountant 403, unauth 401) and
property-confined — including the adversarial path where an SSSJ GM reaches
HISJ's standards, forecast, or an HISJ employee's note. Hints are exercised
against directly seeded statistic facts (batch + stage + fact triples — the
fact's stat_stage_id/ingest_batch_id are NOT NULL) and against a property
with ZERO facts, which must yield nulls, never a 500.
"""

import hashlib
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.employees import make_employee, set_rate_everywhere
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    Department,
    Employee,
    IngestBatch,
    LaborStandard,
    OccupancyForecast,
    Organization,
    PmsDailyStatisticStage,
    Property,
    UsaliStatisticFact,
)
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from tests.authkit import make_authkit
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

# A Monday on the payroll grid (anchor 2026-01-05, also a Monday).
_WEEK = "2026-07-06"
_WEEK_START = date(2026, 7, 6)


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
    """Two properties. HISJ gets a department + employee; SSSJ gets its own
    department + employee for cross-property refusals."""
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
    faye = make_employee(db_session, property_id="SSSJ", full_name="Faye F", pay_type="hourly")
    db_session.add_all([emp, faye])
    db_session.commit()
    # L4: role authority is DB grants — plant each minted subject's
    # grant (scope still comes from the token claim).
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "accountant", sub="acct")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    return {
        "dept": dept.department_id, "foreign_dept": foreign_dept.department_id,
        "emp": emp.employee_id, "faye": faye.employee_id,
    }


def _seed_stat_facts(db_session, values, *, property_id="HISJ",
                     metric_code="ROOMS_OCCUPIED", period="DAY",
                     is_prior_year=False):
    """Insert ROOMS_OCCUPIED-style facts directly, one per (date, value). Each
    fact needs its batch + statistic-stage row first (NOT-NULL FKs) — the
    test_reporting_sos `_synthetic_fact` pattern, on the STATISTIC tables."""
    batch = IngestBatch(
        pms_source="OPERA", report_type="manager_flash",
        source_file="synthetic.pdf", file_hash="0" * 64,
    )
    db_session.add(batch)
    db_session.flush()
    for business_date, value in values.items():
        row_hash = hashlib.sha256(
            f"{property_id}|{business_date}|{metric_code}|{period}|"
            f"{is_prior_year}|{value}".encode()
        ).hexdigest()
        stage = PmsDailyStatisticStage(
            property_id=property_id, pms_source="OPERA",
            report_type="manager_flash", business_date=business_date,
            metric_label="Rooms Occupied", period_label=period,
            is_prior_year=is_prior_year, value=value,
            source_file="synthetic.pdf", ingest_batch_id=batch.batch_id,
            row_hash=row_hash,
        )
        db_session.add(stage)
        db_session.flush()
        db_session.add(UsaliStatisticFact(
            property_id=property_id, pms_source="OPERA",
            business_date=business_date, metric_code=metric_code,
            period=period, is_prior_year=is_prior_year, value=value,
            ingest_batch_id=batch.batch_id, stat_stage_id=stage.stat_stage_id,
        ))
    db_session.commit()


def _admin(mint):
    return {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}


def _gm(mint, prop="HISJ", sub="gm"):
    tok = mint(roles=["property_gm"], sub=sub,
               scopes=[{"property_id": prop, "department_id": None}])
    return {"Authorization": f"Bearer {tok}"}


def _standard(ids, **overrides):
    body = {"property": "HISJ", "department_id": ids["dept"],
            "basis": "fixed_hours_per_day", "value": 16.0}
    body.update(overrides)
    return body


def _forecast_days(week_start=_WEEK_START, rooms=(40, 41, 42, 43, 44, 45, 46)):
    return [
        {"business_date": (week_start + timedelta(days=i)).isoformat(),
         "occupied_rooms": rooms[i]}
        for i in range(len(rooms))
    ]


# --- standards -------------------------------------------------------------


def test_standard_upsert_round_trip(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    r = c.put("/api/schedule/standards", headers=admin, json=_standard(ids))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["property_id"] == "HISJ"
    assert body["department_id"] == ids["dept"]
    assert body["basis"] == "fixed_hours_per_day"
    assert body["value"] == "16.00"  # the 2dp numeric-string convention
    std_id = body["standard_id"]

    listed = c.get("/api/schedule/standards", params={"property": "HISJ"},
                   headers=admin)
    assert listed.status_code == 200
    assert [s["standard_id"] for s in listed.json()] == [std_id]

    # Re-PUT for the SAME department replaces basis + value — one standard per
    # department, same row, no duplicate.
    r = c.put("/api/schedule/standards", headers=admin,
              json=_standard(ids, basis="minutes_per_occupied_room", value=30.0))
    assert r.status_code == 200, r.text
    assert r.json()["standard_id"] == std_id
    assert r.json()["basis"] == "minutes_per_occupied_room"
    assert r.json()["value"] == "30.00"
    assert len(db_session.execute(select(LaborStandard)).scalars().all()) == 1

    assert c.delete(f"/api/schedule/standards/{std_id}",
                    headers=admin).status_code == 204
    assert c.get("/api/schedule/standards", params={"property": "HISJ"},
                 headers=admin).json() == []
    assert c.delete(f"/api/schedule/standards/{std_id}",
                    headers=admin).status_code == 404


def test_standard_validation_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    # Unknown basis is a schema 422 (Literal).
    assert c.put("/api/schedule/standards", headers=admin,
                 json=_standard(ids, basis="hours_per_guest")).status_code == 422
    # A zero or negative value is not a standard.
    for bad in (0, -1):
        assert c.put("/api/schedule/standards", headers=admin,
                     json=_standard(ids, value=bad)).status_code == 422, bad
    # SSSJ's department — and an unknown one — are the same refusal (no
    # existence oracle over another property's department ids).
    for bad in (ids["foreign_dept"], 99999):
        r = c.put("/api/schedule/standards", headers=admin,
                  json=_standard(ids, department_id=bad))
        assert r.status_code == 422, r.text
        assert "SSSJ" not in r.text
    assert db_session.execute(select(LaborStandard)).scalars().all() == []


# --- forecast --------------------------------------------------------------


def test_forecast_upsert_and_one_audit_per_request(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    r = c.put("/api/schedule/forecast", headers=admin,
              json={"property": "HISJ", "days": _forecast_days()})
    assert r.status_code == 200, r.text
    assert r.json() == {"property_id": "HISJ", "saved": 7}

    got = c.get("/api/schedule/forecast",
                params={"property": "HISJ", "week_start": _WEEK}, headers=admin)
    assert got.status_code == 200
    rows = got.json()
    assert [row["business_date"] for row in rows] == [
        (_WEEK_START + timedelta(days=i)).isoformat() for i in range(7)
    ]
    assert [row["occupied_rooms"] for row in rows] == [40, 41, 42, 43, 44, 45, 46]

    # Re-PUT two days: upsert, not duplicate — the other five stand.
    r = c.put("/api/schedule/forecast", headers=admin,
              json={"property": "HISJ", "days": [
                  {"business_date": _WEEK, "occupied_rooms": 55},
                  {"business_date": "2026-07-08", "occupied_rooms": 0},
              ]})
    assert r.status_code == 200
    rows = c.get("/api/schedule/forecast",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=admin).json()
    assert [row["occupied_rooms"] for row in rows] == [55, 41, 0, 43, 44, 45, 46]
    assert len(db_session.execute(select(OccupancyForecast)).scalars().all()) == 7

    # ONE write_occupancy_forecast audit row per REQUEST — two requests, two rows.
    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "write_occupancy_forecast")
    ).scalars().all()
    assert len(audits) == 2
    assert {(a.resource_type, a.resource_id) for a in audits} == {("property", "HISJ")}
    assert {a.actor_subject for a in audits} == {"adm"}


def test_forecast_negative_rooms_is_422(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)

    r = c.put("/api/schedule/forecast", headers=_admin(mint),
              json={"property": "HISJ", "days": [
                  {"business_date": _WEEK, "occupied_rooms": -1},
              ]})
    assert r.status_code == 422
    assert db_session.execute(select(OccupancyForecast)).scalars().all() == []


def test_forecast_hints_math_is_exact(db_engine, db_session, tmp_path):
    """Facts on the 7 days before the week: same-day-last-week is the exact
    per-date value; the trailing average is the mean of those 7 (33 here).
    Prior-year, MTD-period, and other-metric facts are decoys and must not
    move either hint."""
    _seed(db_session)
    week_minus = {  # 2026-06-29 .. 2026-07-05
        _WEEK_START - timedelta(days=7 - i): 30 + i for i in range(7)
    }
    _seed_stat_facts(db_session, week_minus)
    decoy_day = _WEEK_START - timedelta(days=1)
    _seed_stat_facts(db_session, {decoy_day: 999}, is_prior_year=True)
    _seed_stat_facts(db_session, {decoy_day: 888}, period="MTD")
    _seed_stat_facts(db_session, {decoy_day: 777}, metric_code="ADR")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)

    rows = c.get("/api/schedule/forecast",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=_admin(mint)).json()
    # Monday's hint is last Monday's fact (30) ... Sunday's is last Sunday's (36).
    assert [row["hint_same_day_last_week"] for row in rows] == [
        30, 31, 32, 33, 34, 35, 36
    ]
    # Trailing average = mean(30..36) = 33 on every row; no GM entry yet.
    assert all(row["hint_trailing_avg"] == 33 for row in rows)
    assert all(row["occupied_rooms"] is None for row in rows)


def test_forecast_trailing_avg_uses_latest_seven_and_rounds_half_up(
    db_engine, db_session, tmp_path
):
    """Nine facts before the week: only the LATEST 7 average (the two oldest
    drop out), and the mean rounds half-up to an int. Values 10,10 then
    30,31,32,33,34,35,36 offset by +2 -> latest seven are 32..38, mean 35.
    Then a 2-fact case: (30+33)/2 = 31.5 -> 32 (half-up, not banker's)."""
    _seed(db_session)
    nine = {_WEEK_START - timedelta(days=9 - i): v
            for i, v in enumerate([10, 10, 32, 33, 34, 35, 36, 37, 38])}
    _seed_stat_facts(db_session, nine)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    rows = c.get("/api/schedule/forecast",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=admin).json()
    assert rows[0]["hint_trailing_avg"] == 35  # mean(32..38), the 10s dropped

    # Fewer than 7 facts: the mean of what exists, half-up rounded.
    _seed_stat_facts(db_session, {
        _WEEK_START - timedelta(days=2): 30,
        _WEEK_START - timedelta(days=1): 33,
    }, property_id="SSSJ")
    rows = c.get("/api/schedule/forecast",
                 params={"property": "SSSJ", "week_start": _WEEK},
                 headers=admin).json()
    assert rows[0]["hint_trailing_avg"] == 32  # 31.5 rounds HALF-UP
    # And the same-day hint is null where no fact sits 7 days back.
    assert rows[0]["hint_same_day_last_week"] is None


def test_forecast_with_zero_facts_yields_nulls_not_500(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)

    r = c.get("/api/schedule/forecast",
              params={"property": "HISJ", "week_start": _WEEK},
              headers=_admin(mint))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 7
    for row in rows:
        assert row["occupied_rooms"] is None
        assert row["hint_same_day_last_week"] is None
        assert row["hint_trailing_avg"] is None


# --- availability notes ----------------------------------------------------


def test_availability_note_round_trips_and_shows_in_employees(
    db_engine, db_session, tmp_path
):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    r = c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
              headers=admin, json={"note": "can't work Tuesdays"})
    assert r.status_code == 200, r.text
    assert r.json() == {"employee_id": ids["emp"],
                        "availability_note": "can't work Tuesdays"}

    # The note surfaces where the builder's roster comes from: GET /api/employees.
    emps = c.get("/api/employees", headers=admin).json()
    by_id = {e["employee_id"]: e for e in emps}
    assert by_id[ids["emp"]]["availability_note"] == "can't work Tuesdays"
    assert by_id[ids["faye"]]["availability_note"] is None

    # The write is audited.
    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "write_availability_note")
    ).scalars().all()
    assert [a.resource_id for a in audits] == [str(ids["emp"])]
    assert audits[0].resource_type == "employee"

    # Null clears it.
    r = c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
              headers=admin, json={"note": None})
    assert r.status_code == 200
    db_session.expire_all()
    emp = db_session.get(Employee, ids["emp"])
    assert emp is not None and emp.availability_note is None


def test_availability_note_over_300_chars_is_422(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    r = c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
              headers=admin, json={"note": "x" * 301})
    assert r.status_code == 422
    emp = db_session.get(Employee, ids["emp"])
    assert emp is not None and emp.availability_note is None

    # Exactly 300 is the column width — allowed.
    assert c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
                 headers=admin, json={"note": "y" * 300}).status_code == 200


def test_availability_note_unknown_employee_is_404(db_engine, db_session, tmp_path):
    _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    assert c.put("/api/schedule/employees/99999/availability-note",
                 headers=_admin(mint), json={"note": "hi"}).status_code == 404


# --- targets ---------------------------------------------------------------


def _make_week(c, headers, property_id="HISJ", week_start=_WEEK):
    r = c.post("/api/schedule/weeks", headers=headers,
               json={"property": property_id, "week_start": week_start})
    assert r.status_code == 201, r.text
    return r.json()["schedule_id"]


def _add_shift(c, headers, week_id, ids, **overrides):
    body = {"business_date": _WEEK, "department_id": ids["dept"],
            "start_time": "09:00", "end_time": "17:00",
            "crosses_midnight": False, "employee_id": ids["emp"]}
    body.update(overrides)
    r = c.post(f"/api/schedule/weeks/{week_id}/shifts", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()["shift_id"]


def _targets(c, headers, week_id):
    r = c.get(f"/api/schedule/weeks/{week_id}/targets", headers=headers)
    assert r.status_code == 200, r.text
    return r


def test_targets_fixed_standard_is_constant_regardless_of_forecast(
    db_engine, db_session, tmp_path
):
    """fixed_hours_per_day 16.00 -> target "16.00" every day. A partial
    forecast is seeded deliberately: fixed targets must not read it, and
    days_without_forecast stays 0 (the counter is about per-room absence)."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    assert c.put("/api/schedule/standards", headers=admin,
                 json=_standard(ids)).status_code == 200  # fixed 16.00
    assert c.put("/api/schedule/forecast", headers=admin,
                 json={"property": "HISJ", "days": [
                     {"business_date": _WEEK, "occupied_rooms": 40},
                 ]}).status_code == 200
    week_id = _make_week(c, admin)
    # Hank Monday 09:00-17:00: an 8h span, > the 6h meal threshold -> 7.50
    # scheduled hours — the SAME meal-adjusted arithmetic the projection uses.
    _add_shift(c, admin, week_id, ids)

    body = _targets(c, admin, week_id).json()
    assert body["schedule_id"] == week_id
    assert body["week_start"] == _WEEK
    [dept] = body["departments"]
    assert dept["department"] == "Front Desk"
    assert [d["business_date"] for d in dept["days"]] == [
        (_WEEK_START + timedelta(days=i)).isoformat() for i in range(7)
    ]
    assert [d["target_hours"] for d in dept["days"]] == ["16.00"] * 7
    assert dept["target_total"] == "112.00"
    assert dept["days_without_forecast"] == 0
    assert [d["scheduled_hours"] for d in dept["days"]] == ["7.50"] + ["0.00"] * 6
    assert dept["scheduled_total"] == "7.50"


def test_targets_per_room_standard_and_absent_forecast_is_null_not_zero(
    db_engine, db_session, tmp_path
):
    """minutes_per_occupied_room 30.00 x 40 rooms -> "20.00". A day with NO
    forecast -> target_hours null (absence is not zero demand), the week
    total sums the NON-null days only, and days_without_forecast counts the
    nulls — both pinned. Scheduled hours include OPEN shifts (planned
    coverage) and are meal-adjusted."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    assert c.put("/api/schedule/standards", headers=admin,
                 json=_standard(ids, basis="minutes_per_occupied_room",
                                value=30.0)).status_code == 200
    # Monday and Tuesday forecast; the other five days deliberately absent.
    assert c.put("/api/schedule/forecast", headers=admin,
                 json={"property": "HISJ", "days": [
                     {"business_date": _WEEK, "occupied_rooms": 40},
                     {"business_date": "2026-07-07", "occupied_rooms": 50},
                 ]}).status_code == 200
    week_id = _make_week(c, admin)
    # Monday: Hank 09:00-17:00 (7.50 after the meal) + an OPEN 10:00-16:00
    # (6.00, exactly the threshold — no deduction) = 13.50 planned coverage.
    _add_shift(c, admin, week_id, ids)
    _add_shift(c, admin, week_id, ids, employee_id=None,
               start_time="10:00", end_time="16:00")

    body = _targets(c, admin, week_id).json()
    [dept] = body["departments"]
    days = dept["days"]
    assert days[0]["target_hours"] == "20.00"  # 30 min x 40 rooms / 60
    assert days[1]["target_hours"] == "25.00"  # 30 min x 50 rooms / 60
    for day in days[2:]:
        assert day["target_hours"] is None  # null, NOT "0.00"
    assert dept["target_total"] == "45.00"  # sums the two non-null days only
    assert dept["days_without_forecast"] == 5
    assert days[0]["scheduled_hours"] == "13.50"  # open shift INCLUDED
    assert dept["scheduled_total"] == "13.50"


def test_targets_department_without_a_standard_is_null_but_present(
    db_engine, db_session, tmp_path
):
    """A department with shifts but no standard appears with target_hours null
    all week — and days_without_forecast 0: those nulls mean "no standard",
    not "missing forecast"."""
    ids = _seed(db_session)
    hk = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(hk)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    assert c.put("/api/schedule/standards", headers=admin,
                 json=_standard(ids)).status_code == 200  # Front Desk only
    week_id = _make_week(c, admin)
    _add_shift(c, admin, week_id, ids, department_id=hk.department_id,
               employee_id=None, start_time="10:00", end_time="16:00")

    body = _targets(c, admin, week_id).json()
    depts = {d["department"]: d for d in body["departments"]}
    assert set(depts) == {"Front Desk", "Housekeeping"}
    hk_row = depts["Housekeeping"]
    assert [d["target_hours"] for d in hk_row["days"]] == [None] * 7
    assert hk_row["target_total"] is None
    assert hk_row["days_without_forecast"] == 0
    assert hk_row["days"][0]["scheduled_hours"] == "6.00"
    assert hk_row["scheduled_total"] == "6.00"


def test_targets_standard_with_no_shifts_shows_the_unstaffed_target(
    db_engine, db_session, tmp_path
):
    """A department with a standard but no shifts still appears — the GM must
    see unstaffed targets, scheduled 0.00 across the week."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    assert c.put("/api/schedule/standards", headers=admin,
                 json=_standard(ids)).status_code == 200
    week_id = _make_week(c, admin)

    body = _targets(c, admin, week_id).json()
    [dept] = body["departments"]
    assert dept["department"] == "Front Desk"
    assert [d["target_hours"] for d in dept["days"]] == ["16.00"] * 7
    assert [d["scheduled_hours"] for d in dept["days"]] == ["0.00"] * 7
    assert dept["scheduled_total"] == "0.00"
    assert dept["target_total"] == "112.00"


def test_targets_response_carries_no_money(db_engine, db_session, tmp_path):
    """THE MONEY RULE, asserted on the response text: Hank carries the
    distinctive rate 23.17 and his 7.50h Monday would cost 173.78 — neither
    the rate, nor the cost, nor any cost/rate-shaped key may appear. Targets
    are HOURS-only by design."""
    ids = _seed(db_session)
    hank = db_session.get(Employee, ids["emp"])
    assert hank is not None
    # On the PLACEMENT: `hank.pay_rate = ...` set an attribute that no longer
    # maps to a column, which SQLAlchemy accepts silently -- so this leak test
    # would have passed while nothing was priced at all.
    set_rate_everywhere(db_session, hank, "23.17")
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)

    assert c.put("/api/schedule/standards", headers=admin,
                 json=_standard(ids)).status_code == 200
    week_id = _make_week(c, admin)
    _add_shift(c, admin, week_id, ids)  # 7.50h at 23.17 would be 173.78

    r = _targets(c, admin, week_id)
    assert "23.17" not in r.text
    assert "173.78" not in r.text and "173.77" not in r.text
    lowered = r.text.lower()
    assert "cost" not in lowered and "rate" not in lowered
    assert "pay" not in lowered


def test_targets_confinement_and_gating(db_engine, db_session, tmp_path):
    """Confined on the SCHEDULE'S property: the SSSJ GM's touch is a 403 that
    names nothing; the accountant is 403; unauthenticated is 401; an unknown
    week is 404. The property's own GM reads it, of course."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add_shift(c, admin, week_id, ids)

    r = c.get(f"/api/schedule/weeks/{week_id}/targets", headers=_gm(mint, prop="SSSJ"))
    assert r.status_code == 403
    assert "HISJ" not in r.text and "Hank" not in r.text

    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}
    assert c.get(f"/api/schedule/weeks/{week_id}/targets",
                 headers=acct).status_code == 403
    assert c.get(f"/api/schedule/weeks/{week_id}/targets").status_code == 401
    assert c.get("/api/schedule/weeks/99999/targets",
                 headers=admin).status_code == 404
    assert c.get(f"/api/schedule/weeks/{week_id}/targets",
                 headers=_gm(mint, prop="HISJ")).status_code == 200


# --- gating + confinement --------------------------------------------------


def test_gm_is_confined_on_all_three_surfaces(db_engine, db_session, tmp_path):
    """THE adversarial path: an SSSJ GM touches HISJ's standards, forecast, and
    an HISJ employee's note — every touch is a 403 and nothing changes."""
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    sssj_gm = _gm(mint, prop="SSSJ")

    # Standards.
    assert c.put("/api/schedule/standards", headers=sssj_gm,
                 json=_standard(ids)).status_code == 403
    assert c.get("/api/schedule/standards", params={"property": "HISJ"},
                 headers=sssj_gm).status_code == 403
    std_id = c.put("/api/schedule/standards", headers=admin,
                   json=_standard(ids)).json()["standard_id"]
    assert c.delete(f"/api/schedule/standards/{std_id}",
                    headers=sssj_gm).status_code == 403
    assert db_session.get(LaborStandard, std_id) is not None

    # Forecast.
    assert c.put("/api/schedule/forecast", headers=sssj_gm,
                 json={"property": "HISJ",
                       "days": _forecast_days()}).status_code == 403
    assert c.get("/api/schedule/forecast",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=sssj_gm).status_code == 403
    assert db_session.execute(select(OccupancyForecast)).scalars().all() == []

    # Availability note — confined on the EMPLOYEE'S property, and the refusal
    # names neither the employee nor the property.
    r = c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
              headers=sssj_gm, json={"note": "hijacked"})
    assert r.status_code == 403
    assert "Hank" not in r.text and "HISJ" not in r.text
    emp = db_session.get(Employee, ids["emp"])
    assert emp is not None and emp.availability_note is None

    # The property's own GM can use all three, of course.
    hisj_gm = _gm(mint, prop="HISJ")
    assert c.get("/api/schedule/standards", params={"property": "HISJ"},
                 headers=hisj_gm).status_code == 200
    assert c.put("/api/schedule/forecast", headers=hisj_gm,
                 json={"property": "HISJ",
                       "days": _forecast_days()}).status_code == 200
    assert c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
                 headers=hisj_gm, json={"note": "prefers AM"}).status_code == 200


def test_accountant_is_403_and_unauthenticated_is_401(db_engine, db_session, tmp_path):
    ids = _seed(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}

    assert c.put("/api/schedule/standards", headers=acct,
                 json=_standard(ids)).status_code == 403
    assert c.get("/api/schedule/standards", params={"property": "HISJ"},
                 headers=acct).status_code == 403
    assert c.delete("/api/schedule/standards/1", headers=acct).status_code == 403
    assert c.put("/api/schedule/forecast", headers=acct,
                 json={"property": "HISJ",
                       "days": _forecast_days()}).status_code == 403
    assert c.get("/api/schedule/forecast",
                 params={"property": "HISJ", "week_start": _WEEK},
                 headers=acct).status_code == 403
    assert c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
                 headers=acct, json={"note": "no"}).status_code == 403
    assert db_session.execute(select(LaborStandard)).scalars().all() == []
    assert db_session.execute(select(OccupancyForecast)).scalars().all() == []

    assert c.put("/api/schedule/standards", json=_standard(ids)).status_code == 401
    assert c.get("/api/schedule/forecast",
                 params={"property": "HISJ", "week_start": _WEEK}).status_code == 401
    assert c.put(f"/api/schedule/employees/{ids['emp']}/availability-note",
                 json={"note": "no"}).status_code == 401
