"""E3 adversarial-review remediation (three lenses: money, disclosure,
migration/tests). Every test here began life as a reviewer's reproduction of a
finding and FAILED against the pre-remediation code.

The findings, in severity order:

- CRITICAL (money): re-promoting a FILED card after a flip to
  `exclude_from_payroll` silently deleted the filed facts — the restatement
  `terminate_employee` refuses, re-entered through the classification door.
- HIGH (money): the pay-run population filter sampled pay_type NOW, so wages
  earned while hourly vanished from the run with nobody named.
- HIGH (disclosure): the schedule projection never got the exclusion
  predicate — an excluded owner with a rate priced cost, inflated hours
  statistics, and lifted a solo-worker department over the >= 2 floor,
  making the worker's exact rate recoverable by subtracting the owner's
  known compensation. The fix-one-gate-miss-another pattern, again.
- HIGH (disclosure, pre-existing, closed now): kiosk `punch` 404'd unknown
  ids, an existence oracle the other two kiosk endpoints had collapsed.
- MEDIUM (disclosure): I-9/W-4 dates and the completeness flag reached the
  lowest operator tier; now onboarder/payroll-tier only in the list.
- MEDIUM x2 (tests): the PATCH endpoint's whole non-org_admin path was
  untested, and its docstring promised post-termination paperwork writes
  that the 409 branch denies every scoped caller.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from tests.test_payroll_run import _sealed_profile
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

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)


def _seed(db_session, *, schedule=False):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="Attendant",
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
    # L4: role authority is DB grants — the GM tier's compliance
    # visibility now hangs off the grant row, not the token role;
    # 'dm' deliberately gets NO grant (the hidden-fields pin).
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    return dept.department_id, pos.position_id, device.device_id


def _shift(db_session, device_id, emp_id, day, in_h, out_h, month=7):
    for ptype, h in (("clock_in", in_h), ("clock_out", out_h)):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(2026, month, day, h, tzinfo=UTC),
            business_date=date(2026, month, day),
            photo_key=f"k/{emp_id}-{ptype}{day}{h}",
        ))


def _approved_card(db_session, emp_id, period_day=_PERIOD_DAY):
    card = assemble_timecard(db_session, emp_id, period_day, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    return card


# --- CRITICAL: re-promotion after exclusion must REFUSE, not restate ---------


def test_repromotion_of_a_filed_card_after_exclusion_refuses(db_session):
    """Reviewer reproduction: promote an hourly card ($160 filed), flip the
    employee to exclude_from_payroll, re-promote. Pre-fix the filed facts were
    DELETED with only a log line — a closed Schedule 14 stopped tying to what
    was filed, the exact harm the closed-period invariant exists to prevent,
    and the same restatement terminate_employee refuses one function above.

    Post-fix: the re-promote refuses loudly and the facts survive. Resolving a
    real misclassification means correcting the facts deliberately, not
    having a routine backfill eat them."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Flip F",
                        pay_type="hourly", pay_rate="20.00")
    db_session.flush()
    _shift(db_session, device_id, emp.employee_id, 6, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp.employee_id)
    assert promote_timecard(db_session, card, anchor=_ANCHOR) == 1
    db_session.commit()

    db_session.get(Employee, emp.employee_id).pay_type = "exclude_from_payroll"
    db_session.commit()

    with pytest.raises(ValueError, match="filed"):
        promote_timecard(db_session, card, anchor=_ANCHOR)
    db_session.rollback()
    facts = db_session.execute(select(UsaliLaborFact)).scalars().all()
    assert len(facts) == 1, "the filed fact must survive the refused re-promote"
    assert Decimal(str(facts[0].est_cost)) == Decimal("160.0000")


def test_an_always_excluded_card_still_promotes_nothing_quietly(db_session):
    """The guard must not turn the ordinary owner case into noise: a card with
    NO prior facts promotes zero facts and raises nothing."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Olive Owner",
                        pay_type="exclude_from_payroll")
    db_session.flush()
    _shift(db_session, device_id, emp.employee_id, 6, 9, 17)
    db_session.commit()
    card = _approved_card(db_session, emp.employee_id)
    assert promote_timecard(db_session, card, anchor=_ANCHOR) == 0
    db_session.commit()
    assert db_session.execute(select(UsaliLaborFact)).scalars().all() == []


# --- HIGH: earned-while-hourly wages must not vanish from the pay run --------


def test_pay_run_names_an_excluded_employee_with_promoted_facts(db_session):
    """Reviewer reproduction: Wanda earned 8h as an HOURLY employee (facts
    promoted, cost filed), then her pay_type flipped. Pre-fix the population
    filter silently dropped her — report.ok, no entry, no problem line, her
    earned wages never submitted: the E1 dropped-paycheck shape the filter's
    own comment forbids twelve lines above.

    Post-fix: promoted facts are the evidence she was priced in this period,
    and the run REFUSES with a blocker naming her. A from-birth owner has no
    facts (promotion skips them) and still filters silently — pinned by the
    test above and test_pay_run_proceeds_without_the_excluded_employee."""
    dept_id, pos_id, device_id = _seed(db_session, schedule=True)
    hank = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                         position_id=pos_id, full_name="Hank H",
                         pay_type="hourly", pay_rate="20.00")
    wanda = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                          position_id=pos_id, full_name="Wanda W",
                          pay_type="hourly", pay_rate="21.00")
    db_session.flush()
    _sealed_profile(db_session, hank.employee_id)
    _sealed_profile(db_session, wanda.employee_id)
    _shift(db_session, device_id, hank.employee_id, 6, 8, 16)
    _shift(db_session, device_id, wanda.employee_id, 6, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank.employee_id)
    wanda_card = _approved_card(db_session, wanda.employee_id)
    assert promote_timecard(db_session, wanda_card, anchor=_ANCHOR) == 1
    db_session.commit()

    db_session.get(Employee, wanda.employee_id).pay_type = "exclude_from_payroll"
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    assert not report.ok, "earned hours must not vanish behind report.ok"
    named = [p for p in report.problems if "Wanda W" in p]
    assert named and "exclude_from_payroll" in named[0], report.problems
    assert wanda.employee_id not in {e.employee_id for e in report.entries}


# --- HIGH: the projection must not price excluded staff ----------------------


def test_projection_excluded_companion_does_not_lift_suppression(
    db_engine, db_session, tmp_path
):
    """Reviewer reproduction (Rita/Otis): Rita is the only real hourly worker
    in the department (rate 17.71, 6h). Otis is exclude_from_payroll but
    carries a rate on his placement (rates are independent of pay_type) and is
    scheduled 6h. Pre-fix the projection priced Otis: department disclosed
    est_cost 346.26, and 346.26 - (6 x 40.00) = 106.26, /6h = 17.71 — Rita's
    exact rate, recoverable by anyone who knows the owner's figure.

    Post-fix Otis is unpriced everywhere in the projection: cost accumulates
    nothing, the priced count stays 1, the department suppresses. Hours stay —
    the schedule view reports scheduled reality."""
    from tests.test_schedule_api import (
        _add,
        _admin,
        _client,
        _make_week,
        _projection,
        _seed as _sched_seed,
        _seed_costing,
        _shift as _sched_shift,
    )
    from tests.employees import set_rate
    from usali.models import EmployeeAssignment

    ids = _seed_costing(db_session, _sched_seed(db_session))
    rita = make_employee(db_session, property_id="HISJ", full_name="Rita R",
                         pay_type="hourly", pay_rate="17.71")
    otis = make_employee(db_session, property_id="HISJ", full_name="Otis Owner",
                         pay_type="exclude_from_payroll")
    db_session.flush()
    otis_assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == otis.employee_id
        )
    ).scalar_one()
    set_rate(db_session, otis_assignment, "40.00")
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = _admin(mint)
    week_id = _make_week(c, admin)
    _add(c, admin, week_id, _sched_shift(ids, employee_id=rita.employee_id,
                                         start_time="10:00", end_time="16:00"))
    _add(c, admin, week_id, _sched_shift(ids, employee_id=otis.employee_id,
                                         start_time="09:00", end_time="15:00"))

    r = _projection(c, admin, week_id)
    body = r.json()
    [dept] = body["departments"]
    assert dept["est_cost"] is None, (
        "one priced employee: disclosed - owner's known pay = Rita's exact rate"
    )
    assert dept["hours"] == "12.00"  # scheduled reality, both people
    assert body["suppressed_departments"] == 1
    assert body["unpriced_hours"] == "6.00"  # Otis's — priced nowhere
    assert "17.71" not in r.text and "346.26" not in r.text and "240.00" not in r.text


# --- HIGH: kiosk punch was an existence oracle -------------------------------


def test_punch_unknown_id_is_the_shared_403(db_engine, db_session, tmp_path):
    """Pre-existing (BACKLOG'd) and now closed: punch 404'd an unknown id
    before its scope check, so a device could enumerate which employee_ids
    exist (404 = absent, 403 = present). All three kiosk endpoints now give
    the byte-identical 403 for unknown, other-property, and every non-active
    status."""
    from usali.kiosk import mint_device_token
    from usali.photo_store import InMemoryPhotoStore

    dept_id, pos_id, _d = _seed(db_session)
    token, token_hash = mint_device_token()
    device = KioskDevice(property_id="HISJ", name="iPad2",
                         token_hash=token_hash, enrolled_by="adm")
    db_session.add(device)
    leave = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                          position_id=pos_id, full_name="Away A",
                          pay_type="hourly", employment_status="leave")
    db_session.commit()

    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    c = TestClient(app)

    def _punch(emp_id):
        return c.post(
            "/api/kiosk/punch",
            data={"employee_id": emp_id, "punch_type": "clock_in"},
            files={"photo": ("p.jpg", b"\xff\xd8\xff\xe0 fake", "image/jpeg")},
            headers={"X-Kiosk-Token": token},
        )

    unknown = _punch(999999)
    denied = _punch(leave.employee_id)
    assert unknown.status_code == denied.status_code == 403
    assert unknown.json() == denied.json(), (
        "404 vs 403 is an existence oracle: absent vs present-but-refused"
    )


# --- MEDIUM: compliance fields are onboarder/payroll-tier, not operator-tier --


def _app_client(db_engine, tmp_path):
    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app), mint


def test_compliance_fields_are_hidden_from_department_managers(
    db_engine, db_session, tmp_path
):
    """A department manager sees WHO is on leave (scheduling needs it) but not
    I-9/W-4 submission tracking or payroll-readiness — HR-compliance data with
    no supervisor use, held to the same tiering instinct that keeps
    compensation payroll_admin-gated. GMs (the onboarder tier that WRITES
    these fields) still see them."""
    dept_id, pos_id, _d = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=dept_id,
                        position_id=pos_id, full_name="Edith E",
                        pay_type="hourly", employment_status="leave",
                        i9_submitted_on=date(2026, 3, 1),
                        w4_submitted_on=date(2026, 3, 2))
    db_session.commit()
    c, mint = _app_client(db_engine, tmp_path)

    dm = {"Authorization": f"Bearer {mint(roles=['department_manager'], sub='dm', scopes=[{'property_id': 'HISJ', 'department_id': dept_id}])}"}
    r = c.get("/api/employees", headers=dm)
    assert r.status_code == 200
    [row] = [e for e in r.json() if e["employee_id"] == emp.employee_id]
    assert row["employment_status"] == "leave"  # operational, stays visible
    assert row["i9_submitted_on"] is None
    assert row["w4_submitted_on"] is None
    assert row["payroll_data_complete"] is None

    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}
    r = c.get("/api/employees", headers=gm)
    [row] = [e for e in r.json() if e["employee_id"] == emp.employee_id]
    assert row["i9_submitted_on"] == "2026-03-01"
    assert row["payroll_data_complete"] is True


# --- MEDIUM: the PATCH non-org_admin path, tested at last --------------------


def _two_property_seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add_all([
        Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA",
                 wage_jurisdiction="US-CA"),
        Property(property_id="SSSJ", org_id=1, name="SSSJ", pms_source="AUTOCLERK",
                 wage_jurisdiction="US-CA"),
    ])
    db_session.flush()
    d1 = Department(property_id="HISJ", name="Front Desk")
    d2 = Department(property_id="SSSJ", name="Front Desk")
    db_session.add_all([d1, d2])
    db_session.flush()
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    return d1.department_id, d2.department_id


def _gm(mint, prop="HISJ", sub="gm"):
    return {"Authorization": f"Bearer {mint(roles=['property_gm'], sub=sub, scopes=[{'property_id': prop, 'department_id': None}])}"}


def test_patch_scope_for_property_gms(db_engine, db_session, tmp_path):
    """The whole non-org_admin branch, previously untested: in-scope 200 (with
    audit), out-of-scope 403 — including the case the code comment warns
    about: a TWO-property employee must need scope over EVERY placement, so a
    GM holding one hotel is refused (an `all` -> `any` regression dies
    here)."""
    from tests.employees import place
    from usali.models import AuditEvent

    d1, _d2 = _two_property_seed(db_session)
    solo = make_employee(db_session, property_id="HISJ", department_id=d1,
                         full_name="Solo S", pay_type="hourly")
    both = make_employee(db_session, property_id="HISJ", department_id=d1,
                         full_name="Both B", pay_type="hourly")
    db_session.flush()
    place(db_session, both, property_id="SSSJ", is_primary=False)
    db_session.commit()
    c, mint = _app_client(db_engine, tmp_path)

    r = c.patch(f"/api/employees/{solo.employee_id}", headers=_gm(mint),
                json={"full_part_time": "part_time"})
    assert r.status_code == 200, r.text
    audit = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "update_employee")
    ).scalars().all()
    assert len(audit) == 1

    r = c.patch(f"/api/employees/{solo.employee_id}", headers=_gm(mint, prop="SSSJ", sub="gm2"),
                json={"full_part_time": "full_time"})
    assert r.status_code == 403

    r = c.patch(f"/api/employees/{both.employee_id}", headers=_gm(mint),
                json={"full_part_time": "full_time"})
    assert r.status_code == 403, (
        "scope over ONE hotel must not edit the record the other hotel's "
        "kiosk and pay run read"
    )


def test_patch_on_a_terminated_employee_is_org_admin_work(db_engine, db_session, tmp_path):
    """Termination closes every placement (decision 2), so a scoped GM has
    nothing to scope a post-termination edit against and gets the 409 —
    deliberately: with no placements there is no way to verify the caller's
    authority over this person. Post-termination paperwork (the late W-4) is
    org_admin work. Pinned so the docstring and the behaviour agree."""
    from usali.onboarding import terminate_employee

    d1, _d2 = _two_property_seed(db_session)
    emp = make_employee(db_session, property_id="HISJ", department_id=d1,
                        full_name="Gone G", pay_type="hourly")
    db_session.commit()
    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp.employee_id,
                       actor_subject="adm", on_date=date(2026, 7, 10))
    db_session.commit()
    c, mint = _app_client(db_engine, tmp_path)

    r = c.patch(f"/api/employees/{emp.employee_id}", headers=_gm(mint),
                json={"w4_submitted_on": "2026-07-11"})
    assert r.status_code == 409

    adm = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    r = c.patch(f"/api/employees/{emp.employee_id}", headers=adm,
                json={"w4_submitted_on": "2026-07-11"})
    assert r.status_code == 200
