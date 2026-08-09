"""E3: classification and compliance fields — behavioural tests.

`exclude_from_payroll` is a PAY TYPE, not a boolean: an excluded person (an
owner drawing no wage — four El Sendero staff today) produces NO labor facts
and appears in NO pay run, by design. The tests here pin the two halves of
that population change and the validation that keeps the value set closed.

Excluded means NO ROWS, not zero-cost rows: a zero-cost fact would put an
owner's hours into Schedule 15 and into the denominator of every hours
statistic. Contrast with FLSA-exempt staff, whose hours DO promote (est_cost
0) because a salaried manager's hours are real labor the statistics must see.
"""

from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.labor import promote_timecard
from usali.models import (
    Department,
    Employee,
    KioskDevice,
    Organization,
    PaySchedule,
    Position,
    Property,
    Punch,
    UsaliLaborFact,
)
from usali.payroll_run import assemble_pay_run_entries
from usali.server import create_app
from usali.timecards import assemble_timecard
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)  # Monday
_PERIOD_DAY = date(2026, 7, 6)


def _seed(db_session, *, schedule=False):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Room Attendant",
                   flsa_exempt=False)
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="adm")
    db_session.add_all([pos, device])
    if schedule:
        db_session.add(PaySchedule(
            property_id="HISJ", frequency="biweekly", anchor=_ANCHOR,
            check_date_offset_days=5,
        ))
    db_session.flush()
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "org_admin", sub="adm")
    return dept.department_id, pos.position_id, device.device_id


def _shift(db_session, device_id, emp_id, day, in_h, out_h, month=1):
    for ptype, h in (("clock_in", in_h), ("clock_out", out_h)):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(2026, month, day, h, tzinfo=UTC),
            business_date=date(2026, month, day),
            photo_key=f"k/{emp_id}-{ptype}{day}{h}",
        ))


def _approved_card(db_session, emp_id, period_day):
    card = assemble_timecard(db_session, emp_id, period_day, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    return card


# --- promotion: no facts at all ----------------------------------------------


def test_promotion_writes_no_facts_for_an_excluded_employee(db_session):
    """ZERO rows — not zero-cost rows. The distinguishing assertion is the
    COUNT: an hourly employee with a missing rate also costs nothing, but
    their hours still produce facts. Exclusion must not."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Olive Owner",
                        pay_type="exclude_from_payroll")
    db_session.flush()
    _shift(db_session, device_id, emp.employee_id, 5, 9, 17)
    _shift(db_session, device_id, emp.employee_id, 6, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp.employee_id, date(2026, 1, 5))

    written = promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.commit()

    facts = db_session.execute(select(UsaliLaborFact)).scalars().all()
    assert written == 0
    assert facts == [], (
        "an excluded employee's hours must not enter Schedule 15 or any "
        "hours statistic — no rows, not zero-cost rows"
    )


# NOTE: this file once pinned "re-promotion after exclusion DELETES the stale
# facts". The E3 adversarial review reproduced that as a CRITICAL — deleting
# filed facts on a routine re-promote silently restates a closed Schedule 14,
# the harm terminate_employee's stranded-day guard refuses. The behaviour is
# now a refusal, pinned in tests/test_e3_review_remediation.py.


# --- pay run: filtered from the population, silently and deliberately --------


def test_pay_run_proceeds_without_the_excluded_employee(db_session):
    """The excluded employee has a placement, punches, and an approved card —
    everything that would put them in the run — and NO rate and NO sealed
    profile, either of which would be a named blocker if they entered the
    per-employee checks. The run must proceed as if they were not there:
    being excluded is by design, and a preflight line for a permanent
    condition trains people to ignore preflight lines."""
    from tests.test_payroll_run import _sealed_profile

    dept_id, pos_id, device_id = _seed(db_session, schedule=True)
    hank = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Hank H",
                         pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _sealed_profile(db_session, hank.employee_id)
    olive = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                          position_id=pos_id, full_name="Olive Owner",
                          pay_type="exclude_from_payroll")
    db_session.flush()
    _shift(db_session, device_id, hank.employee_id, 6, 8, 16, month=7)
    _shift(db_session, device_id, olive.employee_id, 6, 8, 16, month=7)
    db_session.commit()
    _approved_card(db_session, hank.employee_id, _PERIOD_DAY)
    _approved_card(db_session, olive.employee_id, _PERIOD_DAY)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert report.ok, report.problems
    assert [e.employee_id for e in report.entries] == [hank.employee_id]
    assert not any("Olive" in p or str(olive.employee_id) in p
                   for p in report.problems), (
        "exclusion is by design — it must not surface as a blocker"
    )


# --- suppression re-verification: exclusion reshapes the priced population ---


def test_a_department_of_one_priced_plus_one_excluded_stays_dark(
    db_session, seed_six_pdfs
):
    """E3 gives 'not in the priced population' a new cause: exclusion. A
    department whose only companion to one hourly worker is an excluded owner
    is a one-priced-person department, and its cost IS that person's pay
    (cost / hours = their exact rate). The gates must suppress it.

    Today the excluded owner produces NO facts, so even a headcount gate could
    not count them — but that is one mutation away: revert labor's skip to
    zero-cost rows (the exempt shape) AND count presence instead of
    est_cost > 0 (the D1 bug), and this department reads two. This test is
    the fence that pair of regressions would have to break."""
    from decimal import Decimal

    from tests.test_e2_suppression_gates import _housekeeping, _line, _worker
    from usali.reporting import summary_operating_statement

    worked = date(2026, 7, 7)
    dept, pos, device = _housekeeping(db_session)
    _worker(db_session, dept, pos, device, "Paula Priced", rate="23.17")
    _worker(db_session, dept, pos, device, "Olive Owner", rate=None,
            pay_type="exclude_from_payroll")

    sos = summary_operating_statement(db_session, property_id="HISJ",
                                      business_date=worked)
    line = _line(sos)
    assert line.est_cost is None, (
        "one priced employee: cost / hours would be Paula's exact rate"
    )
    assert Decimal(str(line.hours)) == Decimal("8.00"), (
        "Schedule 15 carries Paula's hours ONLY — the owner's day must not "
        "inflate the denominator anyone divides by"
    )


# --- payroll_data_complete: incomplete is a NAMED blocker, not a silent drop --


def test_an_incomplete_employee_is_a_named_preflight_blocker(db_session):
    """The flag blocks INCLUSION via a blocker naming the employee — NOT via
    the population query. A filtered id vanishes from the run with nothing
    naming them, which is the E1 dropped-paycheck shape; a blocker is a
    payroll admin's to-do item."""
    from tests.test_payroll_run import _sealed_profile

    dept_id, pos_id, device_id = _seed(db_session, schedule=True)
    hank = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Hank H",
                         pay_type="hourly", pay_rate="20.00")
    ida = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Ida Incomplete",
                        pay_type="hourly", pay_rate="21.00",
                        payroll_data_complete=False)
    db_session.flush()
    _sealed_profile(db_session, hank.employee_id)
    _sealed_profile(db_session, ida.employee_id)
    _shift(db_session, device_id, hank.employee_id, 6, 8, 16, month=7)
    _shift(db_session, device_id, ida.employee_id, 6, 8, 16, month=7)
    db_session.commit()
    _approved_card(db_session, hank.employee_id, _PERIOD_DAY)
    _approved_card(db_session, ida.employee_id, _PERIOD_DAY)

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok
    named = [p for p in report.problems if "Ida Incomplete" in p]
    assert named and "payroll data" in named[0], report.problems
    assert not any("Hank" in p for p in report.problems)

    db_session.get(Employee, ida.employee_id).payroll_data_complete = True
    db_session.commit()
    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert report.ok, report.problems
    assert {e.employee_id for e in report.entries} == {
        hank.employee_id, ida.employee_id
    }


def test_a_newly_onboarded_employee_starts_incomplete(db_engine, db_session, tmp_path):
    """The MODEL default, observed through the real onboarding path: nobody
    has stated the paperwork is in, so a new hire is blocked from pay runs
    until someone PATCHes the flag true."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.commit()
    grant_role(db_session, "org_admin", sub="adm")  # L4: DB-backed authority
    c, mint = _client(db_engine, tmp_path)
    hdr = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    r = c.post("/api/employees", json={
        "full_name": "New N", "property": "HISJ", "pay_type": "hourly",
    }, headers=hdr)
    assert r.status_code == 201
    emp_id = r.json()["employee_id"]
    db_session.expire_all()
    refreshed = db_session.get(Employee, emp_id)
    assert refreshed.payroll_data_complete is False
    assert refreshed.employment_status == "active"


# --- the value set is closed at the door -------------------------------------


def _client(db_engine, tmp_path):
    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app), mint


def test_onboarding_refuses_an_unknown_pay_type(db_engine, db_session, tmp_path):
    """Today the endpoint accepts any string and lets the column length refuse
    it — wrongly and late. The refusal must name the allowed values and
    withhold the submitted one (roster_seed's message shape: the value may be
    a paste accident carrying something that doesn't belong in a log)."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.commit()
    grant_role(db_session, "org_admin", sub="adm")  # L4: DB-backed authority
    c, mint = _client(db_engine, tmp_path)
    r = c.post(
        "/api/employees",
        json={"full_name": "Typo T", "property": "HISJ", "pay_type": "contractor"},
        headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "contractor" not in detail
    assert "hourly" in detail and "exclude_from_payroll" in detail
    assert db_session.execute(select(Employee)).scalars().all() == []


# --- employment_status: the enum answers the status question -----------------


def test_terminate_employee_records_the_enum_beside_the_date(db_session):
    """The CHECK makes forgetting impossible, but a constraint error surfacing
    as a 500 is not a behaviour — the function must write both, the way every
    caller observes them."""
    from usali.onboarding import terminate_employee

    dept_id, pos_id, _device = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Gone G",
                        pay_type="hourly", pay_rate="20.00")
    db_session.commit()
    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp.employee_id,
                       actor_subject="adm", on_date=date(2026, 7, 10))
    db_session.commit()
    refreshed = db_session.get(Employee, emp.employee_id)
    assert refreshed.employment_status == "terminated"
    assert refreshed.termination_date == date(2026, 7, 10)


def _kiosk_app(db_engine, tmp_path):
    from usali.photo_store import InMemoryPhotoStore

    return create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )


def _kiosk_seed(db_session, *, status):
    from usali.kiosk import mint_device_token

    dept_id, pos_id, _device = _seed(db_session)
    token, token_hash = mint_device_token()
    device = KioskDevice(property_id="HISJ", name="iPad2", token_hash=token_hash,
                         enrolled_by="adm")
    db_session.add(device)
    away = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Away A",
                         pay_type="hourly", pay_rate="20.00",
                         employment_status=status)
    here = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Here H",
                         pay_type="hourly", pay_rate="20.00")
    db_session.commit()
    return token, away.employee_id, here.employee_id


def test_a_leave_employee_is_not_on_the_kiosk_roster(db_engine, db_session, tmp_path):
    """The kiosk admits `active` only (E3 decision 3). Someone on leave who
    taps in is a data-quality event, not a shift — letting the punch through
    silently contradicts the status record."""
    token, away_id, here_id = _kiosk_seed(db_session, status="leave")
    c = TestClient(_kiosk_app(db_engine, tmp_path))
    r = c.get("/api/kiosk/employees", headers={"X-Kiosk-Token": token})
    assert r.status_code == 200
    ids = [e["employee_id"] for e in r.json()]
    assert here_id in ids
    assert away_id not in ids


def test_a_leave_employee_cannot_punch_and_the_denial_is_the_shared_403(
    db_engine, db_session, tmp_path
):
    """Same status, same body as every other kiosk denial — a distinct error
    for 'on leave' would make the kiosk an employment-status oracle."""
    token, away_id, _here = _kiosk_seed(db_session, status="inactive")
    c = TestClient(_kiosk_app(db_engine, tmp_path))

    def _photo():
        return {"photo": ("p.jpg", b"\xff\xd8\xff\xe0 fake", "image/jpeg")}

    denied = c.post("/api/kiosk/punch",
                    data={"employee_id": away_id, "punch_type": "clock_in"},
                    files=_photo(), headers={"X-Kiosk-Token": token})
    assert denied.status_code == 403
    my_week_denied = c.get(
        "/api/kiosk/my-week",
        params={"employee_id": away_id, "week_start": "2026-07-20"},
        headers={"X-Kiosk-Token": token},
    )
    my_week_unknown = c.get(
        "/api/kiosk/my-week",
        params={"employee_id": 999999, "week_start": "2026-07-20"},
        headers={"X-Kiosk-Token": token},
    )
    assert my_week_denied.status_code == my_week_unknown.status_code == 403
    assert my_week_denied.json() == my_week_unknown.json()


# --- PATCH /employees/{id}: the write surface for the new fields -------------


def _patch_setup(db_engine, db_session, tmp_path):
    dept_id, pos_id, _device = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Edith E",
                        pay_type="hourly", pay_rate="20.00")
    db_session.commit()
    c, mint = _client(db_engine, tmp_path)
    hdr = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    return c, hdr, emp.employee_id


def test_patch_updates_classification_fields_and_audits(db_engine, db_session, tmp_path):
    from usali.models import AuditEvent

    c, hdr, emp_id = _patch_setup(db_engine, db_session, tmp_path)
    r = c.patch(f"/api/employees/{emp_id}", headers=hdr, json={
        "employment_status": "leave",
        "full_part_time": "part_time",
        "i9_submitted_on": "2026-07-01",
        "payroll_data_complete": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["employment_status"] == "leave"
    assert body["full_part_time"] == "part_time"
    assert body["i9_submitted_on"] == "2026-07-01"
    assert body["w4_submitted_on"] is None  # omitted → untouched
    assert body["payroll_data_complete"] is True
    db_session.expire_all()
    refreshed = db_session.get(Employee, emp_id)
    assert refreshed.employment_status == "leave"
    audit = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "update_employee")
    ).scalars().all()
    assert len(audit) == 1
    assert audit[0].resource_id == str(emp_id)


def test_patch_refuses_terminated_in_both_directions(db_engine, db_session, tmp_path):
    """`terminated` is entered ONLY through terminate_employee — it is the one
    path that closes assignments and runs the stranded-day refusal. And once
    terminated, no status write may quietly resurrect the record: rehire
    reopens Keycloak, assignments, and rates together, and is its own phase."""
    from usali.onboarding import terminate_employee

    c, hdr, emp_id = _patch_setup(db_engine, db_session, tmp_path)
    r = c.patch(f"/api/employees/{emp_id}", headers=hdr,
                json={"employment_status": "terminated"})
    assert r.status_code == 422
    assert "terminate" in r.json()["detail"]

    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp_id,
                       actor_subject="adm", on_date=date(2026, 7, 10))
    db_session.commit()
    r = c.patch(f"/api/employees/{emp_id}", headers=hdr,
                json={"employment_status": "active"})
    assert r.status_code == 422
    # A non-status field on a terminated employee is still writable — a W-4
    # that surfaces after someone leaves is ordinary paperwork.
    r = c.patch(f"/api/employees/{emp_id}", headers=hdr,
                json={"w4_submitted_on": "2026-07-11"})
    assert r.status_code == 200


def test_patch_refuses_values_outside_the_enums(db_engine, db_session, tmp_path):
    c, hdr, emp_id = _patch_setup(db_engine, db_session, tmp_path)
    assert c.patch(f"/api/employees/{emp_id}", headers=hdr,
                   json={"employment_status": "retired"}).status_code == 422
    assert c.patch(f"/api/employees/{emp_id}", headers=hdr,
                   json={"full_part_time": "casual"}).status_code == 422


def test_the_employee_list_surfaces_the_new_fields(db_engine, db_session, tmp_path):
    c, hdr, emp_id = _patch_setup(db_engine, db_session, tmp_path)
    r = c.get("/api/employees", headers=hdr)
    assert r.status_code == 200
    [row] = [e for e in r.json() if e["employee_id"] == emp_id]
    assert row["employment_status"] == "active"
    assert row["payroll_data_complete"] is True  # the fixture's pilot-roster default
    assert "full_part_time" in row and "i9_submitted_on" in row and "w4_submitted_on" in row


def test_onboarding_accepts_exclude_from_payroll(db_engine, db_session, tmp_path):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.commit()
    grant_role(db_session, "org_admin", sub="adm")  # L4: DB-backed authority
    c, mint = _client(db_engine, tmp_path)
    r = c.post(
        "/api/employees",
        json={"full_name": "Olive Owner", "property": "HISJ",
              "pay_type": "exclude_from_payroll"},
        headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"},
    )
    assert r.status_code == 201
