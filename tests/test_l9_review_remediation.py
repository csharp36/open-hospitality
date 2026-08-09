"""L9: the gates PR #32 shipped without tests, plus the tenancy hole under them.

Three things ship in that PR with no coverage at all — `include_inactive` on
the roster, and both `/api/departments` verbs. "No test" is not the same as
"untested-but-fine": each was checked here by MUTATION, i.e. by breaking the
guard and confirming a test goes red. Where a guard could be deleted with the
suite still green, that guard was decoration.

  * `include_inactive` widens the roster to people with NO placement in force.
    They cannot be scoped the usual way (having no effective assignment is the
    whole point), so they fall to a SECOND scope path — `_in_scope_historically`
    — which is the only place in the file where visibility is decided by a row
    that is not in force. It needed its own cross-property pin.
  * `GET /api/departments` must 403 an out-of-scope property rather than return
    an empty list. Empty-list-as-refusal is the failure mode where a client
    renders "no departments" and nobody learns the request was denied.
  * `POST /api/departments` is a WRITE that takes `property_id` from the request
    body, which is what makes the tenancy hole reachable (see l9a0deptfk).

Where these overlap Pillar L, the assertions run on the two-org world through
the RLS-bound app role, so the wall being proved is the database's.
"""

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from tests.orgworld import ORG1_ADMIN, ORG2_ALIAS, rls_client
from usali.auth import ACTIVE_ORG_HEADER
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    Department,
    Employee,
    EmployeeAssignment,
    Organization,
    Property,
    Timecard,
    UsaliLaborFact,
)
from usali.server import create_app


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app)


def _two_properties(db_session):
    """Two hotels in ONE org, each with a department. Same-org on purpose: the
    tenant wall is not what confines a GM here, scope is, and a bug in scope is
    invisible in a world where RLS would have caught it anyway."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add_all([
        Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA",
                 wage_jurisdiction="US-CA"),
        Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK",
                 wage_jurisdiction="US-CA"),
    ])
    db_session.flush()
    his = Department(property_id="HISJ", name="Housekeeping")
    sss = Department(property_id="SSSJ", name="Front Office")
    db_session.add_all([his, sss])
    db_session.commit()
    return his.department_id, sss.department_id


def _gm(mint, db_session, sub, property_id):
    grant_role(db_session, "property_gm", sub=sub, property_id=property_id)
    tok = mint(roles=["property_gm"], sub=sub,
               scopes=[{"property_id": property_id, "department_id": None}])
    return {"Authorization": f"Bearer {tok}"}


# --- include_inactive: the second scope path --------------------------------


def _terminated_at(db_session, property_id, department_id, name):
    """Someone with a CLOSED placement and nothing in force — the shape that
    only `include_inactive` returns."""
    yesterday = date.today() - timedelta(days=1)
    emp = make_employee(
        db_session, property_id=property_id, department_id=department_id,
        full_name=name, pay_type="hourly",
        effective_from=date(2026, 1, 1), effective_to=yesterday,
    )
    db_session.commit()
    return emp.employee_id


def test_inactive_employees_stay_confined_to_the_property_that_employed_them(
    db_engine, db_session, tmp_path
):
    """The pin `include_inactive` shipped without. A terminated employee is
    visible to the hotel that employed them and to nobody else — and the
    ordinary roster does not leak them to either.

    Mutation-checked: replacing `_in_scope_historically` with `return True`
    (the reviewer's probe) turns the SSSJ leg red here.
    """
    his_dept, _sss_dept = _two_properties(db_session)
    gone = _terminated_at(db_session, "HISJ", his_dept, "Gone Gary")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)

    own = _gm(mint, db_session, "gm-his", "HISJ")
    other = _gm(mint, db_session, "gm-sss", "SSSJ")

    def ids(headers, inactive):
        q = "?include_inactive=true" if inactive else ""
        r = c.get(f"/api/employees{q}", headers=headers)
        assert r.status_code == 200, r.text
        return {e["employee_id"] for e in r.json()}

    assert gone not in ids(own, False), "no placement in force: not on the roster"
    assert gone in ids(own, True), "their own hotel keeps them in the inactive list"
    assert gone not in ids(other, True), "another hotel's GM never employed them"
    assert gone not in ids(other, False)


def test_inactive_scope_follows_the_last_placement_not_any_placement(
    db_engine, db_session, tmp_path
):
    """`_in_scope_historically` asks about the LAST placement; the active path
    asks about ANY. That difference is deliberate and this pins which way it
    cuts: someone who moved hotels and then left is the LAST hotel's former
    employee, not both hotels'.

    So the answer to "is `_in_scope_historically` a latent leak" is no — it is
    strictly TIGHTER than the active path, not looser. The one case where it
    reaches a property the person never worked is a FUTURE-dated placement (no
    assignment in force yet, so the newest row wins), and that case is a hire
    the property is about to employ. Narrowing it to `effective_from <= today`
    would hide a new hire from the GM who just hired them, which is worse than
    listing them early — recorded here so the next reader does not re-open it.
    """
    his_dept, sss_dept = _two_properties(db_session)
    emp = make_employee(
        db_session, property_id="HISJ", department_id=his_dept, full_name="Moved Mo",
        pay_type="hourly", effective_from=date(2026, 1, 1), effective_to=date(2026, 3, 31),
    )
    db_session.flush()
    # Then moved to SSSJ, and that placement has now closed too.
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SSSJ", department_id=sss_dept,
        effective_from=date(2026, 4, 1), effective_to=date.today() - timedelta(days=1),
        is_primary=True, status="active",
    ))
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    his = _gm(mint, db_session, "gm-his", "HISJ")
    sss = _gm(mint, db_session, "gm-sss", "SSSJ")

    def rows(headers):
        r = c.get("/api/employees?include_inactive=true", headers=headers)
        assert r.status_code == 200, r.text
        return [e for e in r.json() if e["employee_id"] == emp.employee_id]

    assert rows(sss), "the hotel that employed them LAST keeps them"
    assert rows(sss)[0]["property_id"] == "SSSJ", "the row shows where they last worked"
    assert not rows(his), "the hotel they left first does not keep them forever"


# --- GET /api/departments ----------------------------------------------------


def test_departments_read_403s_an_out_of_scope_property(db_engine, db_session, tmp_path):
    """Refusal, not an empty list. A client that gets `[]` renders "no
    departments" and the operator never learns the request was denied — the
    difference between "this hotel has none" and "this hotel is not yours".

    Mutation-checked: deleting the `allows_property` guard leaves the suite
    green without this test.
    """
    _his_dept, _sss_dept = _two_properties(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    gm = _gm(mint, db_session, "gm-his", "HISJ")

    mine = c.get("/api/departments?property=HISJ", headers=gm)
    assert mine.status_code == 200, mine.text
    assert [d["name"] for d in mine.json()] == ["Housekeeping"]

    theirs = c.get("/api/departments?property=SSSJ", headers=gm)
    assert theirs.status_code == 403, theirs.text
    assert theirs.json() != []


# --- POST /api/departments ---------------------------------------------------


def test_department_create_is_confined_and_validated(db_engine, db_session, tmp_path):
    """The write path's three refusals in one place: another hotel (403), a
    name already taken there (409), and a name that is only whitespace (422).
    The 422 case matters because the endpoint strips before comparing — an
    unstripped "  " would otherwise become a department nobody can name."""
    _his_dept, _sss_dept = _two_properties(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    gm = _gm(mint, db_session, "gm-his", "HISJ")

    ok = c.post("/api/departments", json={"property_id": "HISJ", "name": " Laundry "}, headers=gm)
    assert ok.status_code == 201, ok.text
    assert ok.json()["name"] == "Laundry", "stored stripped, not as typed"

    cross = c.post("/api/departments", json={"property_id": "SSSJ", "name": "Laundry"},
                   headers=gm)
    assert cross.status_code == 403, cross.text

    dupe = c.post("/api/departments", json={"property_id": "HISJ", "name": "Laundry"},
                  headers=gm)
    assert dupe.status_code == 409, dupe.text

    blank = c.post("/api/departments", json={"property_id": "HISJ", "name": "   "}, headers=gm)
    assert blank.status_code == 422, blank.text

    made = db_session.execute(
        select(func.count()).select_from(Department).where(Department.property_id == "HISJ")
    ).scalar_one()
    assert made == 2, "one seeded, one created; the three refusals wrote nothing"


def test_department_create_refuses_a_property_that_does_not_exist(
    db_engine, db_session, tmp_path
):
    """An org_admin has no assignment scope — it holds the whole org — which is
    why the org_admin branch used to skip the property check entirely. It still
    has to answer whether the property is one of ITS OWN, and an id that exists
    nowhere gets the same words as one that belongs elsewhere: which of the two
    it was is not the caller's business."""
    _two_properties(db_session)
    grant_role(db_session, "org_admin", sub="oa")  # org-wide
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    oa = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='oa')}"}

    r = c.post("/api/departments", json={"property_id": "NOPE", "name": "Spa"}, headers=oa)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "property out of scope"


# --- the window cap and the earnings gate -----------------------------------


def test_employee_work_window_is_capped(db_engine, db_session, tmp_path):
    """A caller-chosen window is a differencing instrument: two legal reads
    subtract to the days between them. The cap does not stop that — nothing
    does — it just puts a floor under how many AUDITED reads a sweep costs."""
    _two_properties(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    gm = _gm(mint, db_session, "gm-his", "HISJ")

    ok = c.get("/api/employees/work?from=2026-01-01&to=2027-02-05", headers=gm)
    assert ok.status_code == 200, ok.text  # exactly 400 days

    too_wide = c.get("/api/employees/work?from=2026-01-01&to=2027-02-06", headers=gm)
    assert too_wide.status_code == 422, too_wide.text
    assert "400 days" in too_wide.json()["detail"]


def test_accountant_gets_hours_without_the_rate(db_engine, db_session, tmp_path):
    """The department manager case is pinned in test_workforce_api; this is the
    other role the rate gate excludes. An accountant reads the STATEMENT, where
    labor cost is a department aggregate — cost per PERSON is one division away
    from that person's pay rate, which is the figure `/pay-rate` refuses them."""
    his_dept, _sss = _two_properties(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=his_dept,
                        full_name="Priced Pat", pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    card = Timecard(employee_id=emp.employee_id, period_start=date(2026, 7, 6),
                    period_end=date(2026, 7, 19), status="approved")
    db_session.add(card)
    db_session.flush()
    db_session.add(UsaliLaborFact(
        property_id="HISJ", business_date=date(2026, 7, 7), department_id=his_dept,
        hours=8, ot_hours=0, est_cost=160, timecard_id=card.timecard_id,
    ))
    grant_role(db_session, "accountant", sub="acct")  # org-wide, sees every property
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}

    r = c.get("/api/employees/work?from=2026-07-01&to=2026-07-31", headers=acct)
    assert r.status_code == 200, r.text
    assert r.json() == [{"employee_id": emp.employee_id, "hours": "8.00",
                         "ot_hours": "0.00", "est_cost": None}]

    named = db_session.execute(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.action == "read_employee_earnings"
        )
    ).scalar_one()
    assert named == 0, "nothing was disclosed, so nobody is named in the trail"

    window = db_session.execute(
        select(AuditEvent.resource_id).where(AuditEvent.action == "read_employee_work")
    ).scalar_one()
    assert window == "*:2026-07-01:2026-07-31", "the trail records WHICH window was read"


# --- the tenancy wall under POST /api/departments ---------------------------


def test_org_admin_cannot_hang_a_department_off_another_orgs_property(
    two_tenant_world, db_url, tmp_path
):
    """The hole l9a0deptfk closes, proved through the real RLS-bound stack.

    Org 1's admin, active in org 1, names org 2's property in the request body.
    Before the fix this created an org-1 department anchored to a hotel org 1
    cannot see: the endpoint skipped the check on the org_admin branch, and the
    single-column FK validated `TWO1` with the referenced table's OWNER
    privileges — i.e. past RLS, which is exactly why a database wall cannot be
    left to notice this on its own.
    """
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    token = mint(roles=["org_admin"], sub=ORG1_ADMIN,
                 organizations=[DEFAULT_ORG_ALIAS, ORG2_ALIAS])
    headers = {"Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: DEFAULT_ORG_ALIAS}

    refused = client.post("/api/departments",
                          json={"property_id": "TWO1", "name": "Poached"}, headers=headers)
    assert refused.status_code == 403, refused.text

    mine = client.post("/api/departments",
                       json={"property_id": "ONE1", "name": "Housekeeping"}, headers=headers)
    assert mine.status_code == 201, mine.text


def test_org_admin_cannot_onboard_into_another_orgs_property(
    two_tenant_world, db_url, tmp_path
):
    """The same hole, the same shape, in `POST /api/employees` — the reviewer
    flagged only the departments verb, but onboarding takes a caller-supplied
    `property` through the identical org_admin branch. Fixing one and not the
    other would have left the more consequential of the two open."""
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    token = mint(roles=["org_admin"], sub=ORG1_ADMIN,
                 organizations=[DEFAULT_ORG_ALIAS, ORG2_ALIAS])
    headers = {"Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: DEFAULT_ORG_ALIAS}

    refused = client.post("/api/employees", json={
        "full_name": "Planted Pete", "property": "TWO1", "pay_type": "hourly",
    }, headers=headers)
    assert refused.status_code == 403, refused.text


def test_department_read_and_write_do_not_cross_the_org_wall(
    two_tenant_world, db_url, tmp_path
):
    """The two-org leg for the departments verbs: org 2's admin creates a
    department at its own property and reads it back; org 1's admin, active in
    org 1, cannot read org 2's property at all. Runs on the app role, so the
    isolation being demonstrated is RLS's and not the ORM's.

    THIS TEST FOUND ITS OWN BUG, and it is the reviewer's "403, not an empty
    list" in a worse key. The `allows_property` guard passes for an org_admin
    (a global-property role allows every id there is), RLS then filtered org
    2's rows away, and the endpoint answered 200 with `[]` — a cross-tenant
    refusal indistinguishable from "that hotel has no departments". Scope
    alone cannot answer this; the property has to be shown to exist HERE. Now
    in `_require_readable_property`, which `require_property_access` shares, so
    the fix is at the door rather than on this one endpoint.
    """
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    w = two_tenant_world

    two = mint(roles=["org_admin"], sub=w.org2_admin,
               organizations=[ORG2_ALIAS, DEFAULT_ORG_ALIAS])
    two_hdr = {"Authorization": f"Bearer {two}", ACTIVE_ORG_HEADER: ORG2_ALIAS}
    made = client.post("/api/departments",
                       json={"property_id": "TWO1", "name": "Banquets"}, headers=two_hdr)
    assert made.status_code == 201, made.text

    read = client.get("/api/departments?property=TWO1", headers=two_hdr)
    assert [d["name"] for d in read.json()] == ["Banquets"]

    one = mint(roles=["org_admin"], sub=ORG1_ADMIN,
               organizations=[DEFAULT_ORG_ALIAS, ORG2_ALIAS])
    one_hdr = {"Authorization": f"Bearer {one}", ACTIVE_ORG_HEADER: DEFAULT_ORG_ALIAS}
    blind = client.get("/api/departments?property=TWO1", headers=one_hdr)
    assert blind.status_code == 403, blind.text


def test_employee_work_does_not_cross_the_org_wall(two_tenant_world, db_url, tmp_path):
    """The two-org leg for the earnings endpoint. Org 1's admin holds the
    rate-editor grants IN ORG 1, so the money question is live — and org 2's
    employee still must not appear, priced or otherwise."""
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    w = two_tenant_world
    token = mint(roles=["org_admin"], sub=ORG1_ADMIN,
                 organizations=[DEFAULT_ORG_ALIAS, ORG2_ALIAS])

    r = client.get("/api/employees/work?from=2026-07-01&to=2026-07-31", headers={
        "Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: DEFAULT_ORG_ALIAS,
    })
    assert r.status_code == 200, r.text
    assert w.org2_emp_id not in {row["employee_id"] for row in r.json()}


def test_department_composite_fk_refuses_a_cross_org_property(
    two_tenant_world, db_session, app_role_engine
):
    """The wall itself, with the endpoint out of the way. An org-1-bound
    session writing a department that names org 2's property is refused by the
    DATABASE — l9a0deptfk's composite (org_id, property_id) has no row to
    match, where the old single-column FK found `TWO1` and let it through."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    from usali.tenancy import bind_org_context

    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, 1)
        s.add(Department(property_id="TWO1", name="Poached"))
        with pytest.raises(IntegrityError):
            s.flush()


def test_the_two_org_world_still_holds_one_employee_each(two_tenant_world, db_session):
    """A guard on the guards: the tests above WRITE into the shared two-org
    world, so this pins that none of them created a person. A departments test
    that quietly onboarded somebody would otherwise drift the world the L7 walk
    asserts against."""
    per_org = dict(
        db_session.execute(
            select(Employee.org_id, func.count()).group_by(Employee.org_id)
        ).all()
    )
    assert per_org == {1: 1, 2: 1}
