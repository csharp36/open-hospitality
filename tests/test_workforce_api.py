from datetime import UTC

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.models import AuditEvent, Department, Organization, Property, QboPushLedger
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
    )
    return TestClient(app)


def _seed_two_properties(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.commit()


def test_gm_scoped_to_own_property_403s_other(db_engine, db_session, tmp_path):
    _seed_two_properties(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], scopes=[{"property_id": "HISJ", "department_id": None}])
    assert c.get("/api/sos?property=HISJ&date=2026-07-07",
                 headers={"Authorization": f"Bearer {tok}"}).status_code != 403
    r = c.get("/api/sos?property=SSSJ&date=2026-07-07",
              headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_accountant_sees_all_properties(db_engine, db_session, tmp_path):
    _seed_two_properties(db_session)
    grant_role(db_session, "accountant")  # L4: org-wide DB grant, not the token
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["accountant"])  # global-property, no scope claim
    assert c.get("/api/sos?property=SSSJ&date=2026-07-07",
                 headers={"Authorization": f"Bearer {tok}"}).status_code != 403


def test_qbo_push_property_scope_enforced(db_engine, db_session, tmp_path):
    _seed_two_properties(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], scopes=[{"property_id": "HISJ", "department_id": None}])
    r = c.post("/api/qbo/push", json={"property": "SSSJ", "date": "2026-07-07"},
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_properties_list_filtered_to_scope(db_engine, db_session, tmp_path, seed_six_pdfs):
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], scopes=[{"property_id": "HISJ", "department_id": None}])
    r = c.get("/api/properties", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    ids = {row["property_id"] for row in r.json()}
    assert ids == {"HISJ"}  # SSSJ filtered out


def _seed_employees(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.flush()
    fo = Department(property_id="HISJ", name="Front Office")
    hk = Department(property_id="HISJ", name="Housekeeping")
    db_session.add_all([fo, hk])
    db_session.flush()
    db_session.add_all([
        make_employee(db_session, property_id="HISJ", department_id=fo.department_id, full_name="A", pay_type="hourly"),
        make_employee(db_session, property_id="HISJ", department_id=hk.department_id, full_name="B", pay_type="hourly"),
        make_employee(db_session, property_id="SSSJ", department_id=None, full_name="C", pay_type="salary"),
    ])
    db_session.commit()
    return fo.department_id, hk.department_id


def test_org_admin_sees_all_employees(db_engine, db_session, tmp_path):
    _seed_employees(db_session)
    grant_role(db_session, "org_admin")  # L4: org-wide DB grant, not the token
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/employees", headers={"Authorization": f"Bearer {mint(roles=['org_admin'])}"})
    assert r.status_code == 200
    assert {e["full_name"] for e in r.json()} == {"A", "B", "C"}


def test_gm_sees_only_own_property(db_engine, db_session, tmp_path):
    _seed_employees(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], scopes=[{"property_id": "HISJ", "department_id": None}])
    r = c.get("/api/employees", headers={"Authorization": f"Bearer {tok}"})
    assert {e["full_name"] for e in r.json()} == {"A", "B"}


def test_department_manager_sees_only_own_department(db_engine, db_session, tmp_path):
    fo_id, _hk_id = _seed_employees(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["department_manager"],
               scopes=[{"property_id": "HISJ", "department_id": fo_id}])
    r = c.get("/api/employees", headers={"Authorization": f"Bearer {tok}"})
    assert {e["full_name"] for e in r.json()} == {"A"}  # Front Office only


def _seed_one_employee_with_pii(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ", full_name="A", pay_type="salary",
                   compensation_note="Base 120000; bonus eligible")
    db_session.add(emp)
    db_session.commit()
    grant_role(db_session, "payroll_admin", sub="pa")  # L4: DB-backed authority
    return emp.employee_id


def test_payroll_admin_reads_pii_and_writes_audit(db_engine, db_session, tmp_path):
    emp_id = _seed_one_employee_with_pii(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get(f"/api/employees/{emp_id}/compensation",
              headers={"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"})
    assert r.status_code == 200
    assert r.json()["compensation_note"] == "Base 120000; bonus eligible"  # decrypted

    n = db_session.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "read_compensation",
            AuditEvent.resource_id == str(emp_id),
            AuditEvent.actor_subject == "pa",
        )
    ).scalar_one()
    assert n == 1


def test_non_payroll_roles_forbidden_from_pii(db_engine, db_session, tmp_path):
    emp_id = _seed_one_employee_with_pii(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    for role in ("accountant", "org_admin", "property_gm", "department_manager", "employee"):
        r = c.get(f"/api/employees/{emp_id}/compensation",
                  headers={"Authorization": f"Bearer {mint(roles=[role])}"})
        assert r.status_code == 403, role


def test_pii_stored_encrypted_at_rest(db_engine, db_session, tmp_path):
    emp_id = _seed_one_employee_with_pii(db_session)
    raw = db_session.execute(
        text("select compensation_note from employee where employee_id = :i"),
        {"i": emp_id},
    ).scalar_one()
    assert "120000" not in raw and raw != "Base 120000; bonus eligible"


def _seed_employee(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ", full_name="A", pay_type="hourly")
    db_session.add(emp)
    db_session.commit()
    grant_role(db_session, "payroll_admin", sub="pa")  # L4: DB-backed authority
    return emp.employee_id


def test_payroll_admin_sets_and_reads_pay_rate(db_engine, db_session, tmp_path):
    emp_id = _seed_employee(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    put = c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa, json={"pay_rate": "27.50"})
    assert put.status_code == 200
    assert put.json()["pay_rate"] == "27.50"

    got = c.get(f"/api/employees/{emp_id}/pay-rate", headers=pa)
    assert got.status_code == 200
    assert got.json()["pay_rate"] == "27.50"

    # Audited: one write + one read.
    from usali.models import AuditEvent
    actions = [a.action for a in db_session.execute(
        select(AuditEvent).where(AuditEvent.resource_id == str(emp_id))
    ).scalars().all()]
    assert "write_pay_rate" in actions and "read_pay_rate" in actions


def test_non_rate_editor_role_cannot_touch_pay_rate(db_engine, db_session, tmp_path):
    """A department manager runs a schedule; they do not set pay.

    This test used to assert the same of a property_gm. That gate was
    deliberately widened — a GM hires, so a GM says what the hire is paid — and
    `test_gm_sets_a_pay_rate_in_its_own_property` covers the new answer. What
    survives unchanged is that the roles OUTSIDE `require_rate_editor` are
    refused, which is what this now pins.
    """
    emp_id = _seed_employee(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    dm = {"Authorization": f"Bearer {mint(roles=['department_manager'], sub='dm',
          scopes=[{'property_id': 'HISJ', 'department_id': 1}])}"}
    assert c.get(f"/api/employees/{emp_id}/pay-rate", headers=dm).status_code == 403
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=dm,
                 json={"pay_rate": "50"}).status_code == 403


def test_pay_rate_rejects_nonpositive_and_absurd(db_engine, db_session, tmp_path):
    emp_id = _seed_employee(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "0"}).status_code == 422
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "-5"}).status_code == 422
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "100000"}).status_code == 422


def test_pay_rate_rejects_sub_cent_precision(db_engine, db_session, tmp_path):
    emp_id = _seed_employee(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "27.12345"}).status_code == 422
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "27.12"}).status_code == 200


def test_qbo_status_filtered_to_scope(db_engine, db_session, tmp_path):
    from datetime import date, datetime
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    # One push-ledger row per property.
    for pid in ("HISJ", "SSSJ"):
        db_session.add(QboPushLedger(
            property_id=pid, business_date=date(2026, 7, 7),
            request_hash=f"h-{pid}", qbo_je_id=None, status="failed",
            message=None, pushed_at=datetime(2026, 7, 7, tzinfo=UTC),
        ))
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], scopes=[{"property_id": "HISJ", "department_id": None}])
    # Omitting the param must NOT dump every property — only in-scope rows.
    r = c.get("/api/qbo/status", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert {row["property_id"] for row in r.json()} == {"HISJ"}
    # Explicitly requesting an out-of-scope property yields nothing (not a leak).
    r2 = c.get("/api/qbo/status?property=SSSJ", headers={"Authorization": f"Bearer {tok}"})
    assert r2.json() == []


def test_setting_a_rate_twice_closes_the_old_row_through_the_real_endpoint(
    db_engine, db_session, tmp_path
):
    """E2 review (Task 7): the endpoint's raise semantics had NO test — the
    close-old/open-new behaviour was pinned only by a re-implementation in
    test_e2_placement_rates. A mutation making the real endpoint overwrite the
    open row in place (reintroducing THE bug E2 exists to kill) killed zero
    tests. This drives the real PUT twice and inspects the rows."""
    from datetime import date

    from usali.models import AssignmentRate, EmployeeAssignment

    emp_id = _seed_employee(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "20.00", "effective_from": "2026-01-05"}
                 ).status_code == 200
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "30.00", "effective_from": "2026-08-01"}
                 ).status_code == 200

    assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == emp_id
        )
    ).scalar_one()
    rows = sorted(
        db_session.execute(
            select(AssignmentRate).where(
                AssignmentRate.assignment_id == assignment.assignment_id
            )
        ).scalars(),
        key=lambda r: r.effective_from,
    )
    assert len(rows) == 2, "the old rate is closed and kept, not overwritten"
    assert rows[0].amount == "20.00"
    assert rows[0].effective_to == date(2026, 8, 1), "closed where the new one opens"
    assert rows[1].amount == "30.00" and rows[1].effective_to is None


def test_backdating_a_rate_over_the_current_one_is_refused(
    db_engine, db_session, tmp_path
):
    """A rate dated on-or-before the current one's start would restate a closed
    period — the whole point of E2. The endpoint must 422, not silently accept."""
    emp_id = _seed_employee(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
                 json={"pay_rate": "20.00", "effective_from": "2026-06-01"}
                 ).status_code == 200
    r = c.put(f"/api/employees/{emp_id}/pay-rate", headers=pa,
              json={"pay_rate": "30.00", "effective_from": "2026-05-01"})
    assert r.status_code == 422, r.text


# --- suspension, editing, and the per-employee work window -------------------
def _seed_two_departments(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA",
                            wage_jurisdiction="US-CA"))
    db_session.flush()
    fo = Department(property_id="HISJ", name="Front Office")
    hk = Department(property_id="HISJ", name="Housekeeping")
    db_session.add_all([fo, hk])
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ", department_id=fo.department_id,
                        full_name="A", pay_type="hourly", pay_rate="20.00")
    db_session.add(emp)
    db_session.commit()
    return emp.employee_id, fo.department_id, hk.department_id


def test_department_move_opens_a_new_placement_and_carries_the_rate(
    db_engine, db_session, tmp_path
):
    """A move must not edit the current placement in place: `department_at`
    resolves it when hours are attributed, so an in-place edit would move hours
    ALREADY promoted under the old department. The rate rides along, because a
    move that silently un-prices someone is worse than either thing it could be
    mistaken for."""
    from datetime import date, timedelta

    from usali.models import AssignmentRate, EmployeeAssignment

    emp_id, fo, hk = _seed_two_departments(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "org_admin", sub="oa")  # org-wide
    gm = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='oa')}"}

    effective = (date.today() + timedelta(days=1)).isoformat()
    r = c.patch(f"/api/employees/{emp_id}",
                json={"full_name": "A Renamed", "department_id": hk,
                      "effective_from": effective},
                headers=gm)
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "A Renamed"

    db_session.expire_all()
    rows = sorted(
        db_session.execute(
            select(EmployeeAssignment).where(EmployeeAssignment.employee_id == emp_id)
        ).scalars(),
        key=lambda a: a.effective_from,
    )
    assert len(rows) == 2, "the old placement is closed and kept, not rewritten"
    assert rows[0].department_id == fo
    assert rows[0].effective_to == date.fromisoformat(effective)
    assert rows[1].department_id == hk and rows[1].effective_to is None
    carried = db_session.execute(
        select(AssignmentRate).where(AssignmentRate.assignment_id == rows[1].assignment_id)
    ).scalar_one()
    assert carried.amount == "20.00", "the same figure, not a rate change"


def test_department_move_into_another_property_is_refused(db_engine, db_session, tmp_path):
    emp_id, _fo, _hk = _seed_two_departments(db_session)
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK",
                            wage_jurisdiction="US-CA"))
    db_session.flush()
    other = Department(property_id="SSSJ", name="Front Office")
    db_session.add(other)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "org_admin", sub="oa")  # org-wide
    gm = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='oa')}"}
    r = c.patch(f"/api/employees/{emp_id}", json={"department_id": other.department_id},
                headers=gm)
    assert r.status_code == 422, r.text


def test_employee_work_window_is_scope_filtered(db_engine, db_session, tmp_path):
    """The per-employee decomposition of Schedule 14. Scope is checked per FACT,
    so a department manager sees the hours their own department bought and not
    the ones the same person worked elsewhere.

    MONEY is a second, narrower question and this test pins both: the GM holds
    the rate-editor grants and gets est_cost; the department manager gets the
    same hours beside a null. See `_WORK_WINDOW_MAX_DAYS` and the endpoint
    docstring for why cost over hours cannot travel wider than the rate gate.
    """
    from datetime import date

    from usali.models import Timecard, UsaliLaborFact

    emp_id, fo, hk = _seed_two_departments(db_session)
    card = Timecard(employee_id=emp_id, period_start=date(2026, 7, 6),
                    period_end=date(2026, 7, 19), status="approved")
    db_session.add(card)
    db_session.flush()
    db_session.add_all([
        UsaliLaborFact(property_id="HISJ", business_date=date(2026, 7, 7), department_id=fo,
                       hours=8, ot_hours=0, est_cost=160, timecard_id=card.timecard_id),
        UsaliLaborFact(property_id="HISJ", business_date=date(2026, 7, 8), department_id=hk,
                       hours=9, ot_hours=1, est_cost=190, timecard_id=card.timecard_id),
        # Outside the window — must not be counted.
        UsaliLaborFact(property_id="HISJ", business_date=date(2026, 6, 30), department_id=fo,
                       hours=8, ot_hours=0, est_cost=160, timecard_id=card.timecard_id),
    ])
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    window = "from=2026-07-01&to=2026-07-31"

    # The GM holds the grant, so the GM sees money. (The grant, not the token
    # role: require_rate_editor asks the same source, and this field has to
    # answer to the same authority as the rate it re-derives.)
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}
    rows = c.get(f"/api/employees/work?{window}", headers=gm).json()
    assert rows == [{"employee_id": emp_id, "hours": "17.00", "ot_hours": "1.00",
                     "est_cost": "350.0000"}]

    grant_role(db_session, "department_manager", sub="dm", property_id="HISJ", department_id=fo)
    dm = {"Authorization": f"Bearer {mint(roles=['department_manager'], sub='dm', scopes=[{'property_id': 'HISJ', 'department_id': fo}])}"}
    rows = c.get(f"/api/employees/work?{window}", headers=dm).json()
    assert rows == [{"employee_id": emp_id, "hours": "8.00", "ot_hours": "0.00",
                     "est_cost": None}], "hours yes, rate no: 160/8 is what /pay-rate withholds"

    n = db_session.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "read_employee_work"
        )
    ).scalar_one()
    assert n == 2, "every read is on the record, disclosed or not"
    earnings = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "read_employee_earnings")
    ).scalars().all()
    assert [(e.actor_subject, e.resource_id) for e in earnings] == [("gm", str(emp_id))], (
        "only the read that actually SHOWED money names the person it showed"
    )


# --- who may set a pay rate --------------------------------------------------


def test_gm_sets_a_pay_rate_in_its_own_property(db_engine, db_session, tmp_path):
    """A GM hires, so a GM says what the hire is paid. Widened from
    payroll_admin-only deliberately (see require_rate_editor)."""
    emp_id = _seed_employee(db_session)
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}

    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=gm,
                 json={"pay_rate": "24.50"}).status_code == 200
    r = c.get(f"/api/employees/{emp_id}/pay-rate", headers=gm)
    assert r.status_code == 200 and r.json()["pay_rate"] == "24.50"


def test_gm_cannot_touch_a_rate_at_another_property(db_engine, db_session, tmp_path):
    """The confinement that makes widening the gate safe. Without it, opening
    the rate to GMs would let a GM at one hotel rewrite pay at the other."""
    emp_id = _seed_employee(db_session)
    # The other hotel has to EXIST for a grant to name it — the composite FK
    # (org_id, property_id) is the tenancy wall doing its job.
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "property_gm", sub="gm2", property_id="SSSJ")
    other = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm2', scopes=[{'property_id': 'SSSJ', 'department_id': None}])}"}

    assert c.get(f"/api/employees/{emp_id}/pay-rate", headers=other).status_code == 403
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=other,
                 json={"pay_rate": "99.00"}).status_code == 403


def test_accountant_cannot_touch_a_pay_rate(db_engine, db_session, tmp_path):
    """Global-property VIEW roles are not rate editors. `accountant` sees every
    property and may set pay at none of them."""
    emp_id = _seed_employee(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "accountant", sub="ac")
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='ac')}"}
    assert c.get(f"/api/employees/{emp_id}/pay-rate", headers=acct).status_code == 403
    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=acct,
                 json={"pay_rate": "99.00"}).status_code == 403


def test_the_pii_vault_stays_payroll_admin_only(db_engine, db_session, tmp_path):
    """The gate that did NOT move. Being allowed to say what someone earns is
    not being allowed to read their SSN — a GM who can now set a rate must still
    be refused the sealed vault and the compensation note."""
    emp_id = _seed_employee(db_session)
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}

    assert c.put(f"/api/employees/{emp_id}/pay-rate", headers=gm,
                 json={"pay_rate": "24.50"}).status_code == 200
    assert c.get(f"/api/employees/{emp_id}/compensation", headers=gm).status_code == 403
    assert c.get(f"/api/payroll/employees/{emp_id}/profile", headers=gm).status_code == 403


# --- labor analytics (payroll dashboard) -------------------------------------


def test_labor_analytics_series_department_rows_and_suppression(
    db_engine, db_session, tmp_path
):
    """The payroll dashboard's one call. Two things it must get right: the day
    series carries the SAME per-day suppression the statement applies, and a
    department with a labor standard shows a target even for days nobody
    worked."""
    from datetime import date

    from usali.models import (
        IngestBatch, LaborStandard, PmsDailyStatisticStage, Timecard,
        UsaliLaborFact, UsaliStatisticFact,
    )

    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA",
                            wage_jurisdiction="US-CA"))
    db_session.flush()
    hk = Department(property_id="HISJ", name="Housekeeping")
    solo = Department(property_id="HISJ", name="Night Auditor")
    db_session.add_all([hk, solo])
    db_session.flush()

    a = make_employee(db_session, property_id="HISJ", department_id=hk.department_id,
                      full_name="A", pay_type="hourly", pay_rate="20.00")
    b = make_employee(db_session, property_id="HISJ", department_id=hk.department_id,
                      full_name="B", pay_type="hourly", pay_rate="22.00")
    c = make_employee(db_session, property_id="HISJ", department_id=solo.department_id,
                      full_name="C", pay_type="hourly", pay_rate="30.00")
    db_session.add_all([a, b, c])
    db_session.flush()

    day = date(2026, 7, 7)
    cards = {}
    for emp in (a, b, c):
        card = Timecard(employee_id=emp.employee_id, period_start=date(2026, 7, 6),
                        period_end=date(2026, 7, 19), status="approved")
        db_session.add(card)
        db_session.flush()
        cards[emp.employee_id] = card.timecard_id
    db_session.add_all([
        UsaliLaborFact(property_id="HISJ", business_date=day, department_id=hk.department_id,
                       hours=8, ot_hours=0, est_cost=160, timecard_id=cards[a.employee_id]),
        UsaliLaborFact(property_id="HISJ", business_date=day, department_id=hk.department_id,
                       hours=8, ot_hours=2, est_cost=176, timecard_id=cards[b.employee_id]),
        # One priced employee: cost suppressed, hours still carried.
        UsaliLaborFact(property_id="HISJ", business_date=day, department_id=solo.department_id,
                       hours=8, ot_hours=0, est_cost=240, timecard_id=cards[c.employee_id]),
    ])

    # A promoted room count so a minutes-per-room standard has a denominator.
    batch = IngestBatch(pms_source="OPERA", report_type="manager_flash", source_file="t",
                        file_hash="h", status="staged", row_count=1, error_count=0)
    db_session.add(batch)
    db_session.flush()
    stage = PmsDailyStatisticStage(
        property_id="HISJ", pms_source="OPERA", report_type="manager_flash",
        business_date=day, metric_label="Rooms Occupied", period_label="DAY",
        is_prior_year=False, value=60, source_file="t", ingest_batch_id=batch.batch_id,
        row_hash="rh1",
    )
    db_session.add(stage)
    db_session.flush()
    db_session.add(UsaliStatisticFact(
        property_id="HISJ", pms_source="OPERA", business_date=day,
        metric_code="ROOMS_OCCUPIED", period="DAY", is_prior_year=False, value=60,
        ingest_batch_id=batch.batch_id, stat_stage_id=stage.stat_stage_id,
    ))
    db_session.add(LaborStandard(
        property_id="HISJ", department_id=hk.department_id,
        basis="minutes_per_occupied_room", value=30,
    ))
    db_session.commit()

    verifier, mint = make_authkit()
    c_api = _client(db_engine, tmp_path, verifier)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}
    r = c_api.get("/api/labor/analytics?property=HISJ&from=2026-07-06&to=2026-07-08", headers=gm)
    assert r.status_code == 200, r.text
    body = r.json()

    assert [d["business_date"] for d in body["days"]] == [
        "2026-07-06", "2026-07-07", "2026-07-08",
    ], "every day in the window appears, worked or not"
    worked = next(d for d in body["days"] if d["business_date"] == "2026-07-07")
    assert worked["hours"] == "24.00"
    assert worked["ot_hours"] == "2.00"
    # 160 + 176 disclosed; the solo department's 240 is NOT in the day total.
    assert worked["est_cost"] == "336.0000"
    assert worked["rooms_occupied"] == "60.0000"
    # Per-day per-department HOURS, which the ranked-department chart draws its
    # trend from. Real per-day figures, not the day total shared out by each
    # department's window share -- that would give every department the same
    # shape and present it as a trend.
    assert worked["department_hours"] == {"Housekeeping": "16.00", "Night Auditor": "8.00"}, (
        "the solo department's HOURS appear per day: hours are never suppressed"
    )
    idle = next(d for d in body["days"] if d["business_date"] == "2026-07-08")
    assert idle["department_hours"] == {}, "a day nobody worked carries no departments"
    # And cost stays out of the per-day breakdown entirely: 240 / 8 would hand
    # back employee C's $30 rate, which `est_cost: None` above exists to refuse.
    assert "department_cost" not in worked

    depts = {d["department"]: d for d in body["departments"]}
    assert depts["Housekeeping"]["est_cost"] == "336.0000"
    assert depts["Night Auditor"]["est_cost"] is None, "one priced employee: hidden"
    assert depts["Night Auditor"]["hours"] == "8.00", "hours are never suppressed"
    # 30 minutes per room x 60 rooms = 30h on the one day with a room count;
    # the other two days have none, and absence is not zero demand.
    assert depts["Housekeeping"]["target_hours"] == "30.00"
    assert body["suppressed_departments"] == 1


def test_labor_analytics_rejects_a_backwards_window(db_engine, db_session, tmp_path):
    _seed_two_properties(db_session)
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}
    r = c.get("/api/labor/analytics?property=HISJ&from=2026-07-31&to=2026-07-01", headers=gm)
    assert r.status_code == 422, r.text


def test_labor_analytics_is_property_confined(db_engine, db_session, tmp_path):
    _seed_two_properties(db_session)
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}
    r = c.get("/api/labor/analytics?property=SSSJ&from=2026-07-01&to=2026-07-02", headers=gm)
    assert r.status_code == 403
