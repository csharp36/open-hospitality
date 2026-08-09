from datetime import UTC, date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.employees import make_employee, set_department, set_rate_everywhere
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    Department,
    Employee,
    KioskDevice,
    Organization,
    Property,
    Punch,
    Timecard,
    TimecardAdjustment,
    UsaliLaborFact,
)
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.timecards import assemble_timecard
from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    device = KioskDevice(property_id="HISJ", name="iPad", token_hash="h" * 64,
                         enrolled_by="adm")
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H", pay_type="hourly")
    db_session.add_all([device, emp])
    db_session.flush()
    # L4: role authority is DB grants — plant each minted subject's
    # grant. SCOPE still comes from the token claim (gm2/gm3 claim SSSJ
    # and must stay confined by it); `rogue` deliberately gets NO row.
    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "accountant")  # the authkit default sub
    grant_role(db_session, "accountant", sub="acct")
    for gm_sub in ("gm", "gm2", "gm3"):
        grant_role(db_session, "property_gm", sub=gm_sub, property_id="HISJ")
    return device.device_id, emp.employee_id


def _punch(db_session, device_id, emp_id, ptype, hour, day=7):
    db_session.add(Punch(
        employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
        punched_at=datetime(2026, 7, day, hour, tzinfo=UTC),
        business_date=date(2026, 7, day), photo_key=f"k/{ptype}{hour}{day}",
    ))


def test_assemble_creates_a_timecard_and_links_punches(db_session):
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    _punch(db_session, device_id, emp_id, "clock_out", 17)
    db_session.commit()

    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    assert card.status == "open"
    assert card.period_start <= date(2026, 7, 7) <= card.period_end
    punches = db_session.execute(select(Punch)).scalars().all()
    assert all(p.timecard_id == card.timecard_id for p in punches)


def test_assemble_is_idempotent(db_session):
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    db_session.commit()

    a = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()
    b = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    assert a.timecard_id == b.timecard_id
    assert len(db_session.execute(select(Timecard)).scalars().all()) == 1


def test_assemble_ignores_punches_outside_the_period(db_session):
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9, day=7)   # in period
    _punch(db_session, device_id, emp_id, "clock_in", 9, day=1)   # prior period
    _punch(db_session, device_id, emp_id, "clock_in", 9, day=25)  # next period
    db_session.commit()

    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    linked = db_session.execute(
        select(Punch).where(Punch.timecard_id == card.timecard_id)
    ).scalars().all()
    assert [p.business_date for p in linked] == [date(2026, 7, 7)]
    unlinked = db_session.execute(
        select(Punch).where(Punch.timecard_id.is_(None))
    ).scalars().all()
    assert {p.business_date for p in unlinked} == {date(2026, 7, 1), date(2026, 7, 25)}


def test_assemble_never_links_another_employees_punches(db_session):
    device_id, emp_id = _seed(db_session)
    other = make_employee(db_session, property_id="HISJ", full_name="Other O", pay_type="hourly")
    db_session.add(other)
    db_session.flush()
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    _punch(db_session, device_id, other.employee_id, "clock_in", 9)
    db_session.commit()

    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    linked = db_session.execute(
        select(Punch).where(Punch.timecard_id == card.timecard_id)
    ).scalars().all()
    assert [p.employee_id for p in linked] == [emp_id]


def _client(db_engine, tmp_path, verifier, store=None):
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=store or InMemoryPhotoStore(),
    )
    return TestClient(app)


def _open_card(db_session):
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    _punch(db_session, device_id, emp_id, "clock_out", 17)
    db_session.commit()
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()
    return card.timecard_id, emp_id


def test_detail_lists_each_days_punches_with_photo_state(db_engine, db_session, tmp_path):
    """The approval view shows each day's punches and fetches photos on
    demand: `has_photo` drives the Photo button, and a purged photo (NULL
    photo_key — the retention job's normal end state) must render as absent
    text, not a broken image the manager reads as data loss."""
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    db_session.add(Punch(  # photo already purged
        employee_id=emp_id, kiosk_device_id=device_id, punch_type="clock_out",
        punched_at=datetime(2026, 7, 7, 17, tzinfo=UTC),
        business_date=date(2026, 7, 7), photo_key=None,
    ))
    db_session.commit()
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get(f"/api/timecards/{card.timecard_id}",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    punches = r.json()["days"][0]["punches"]
    assert [p["punch_type"] for p in punches] == ["clock_in", "clock_out"]
    assert punches[0]["has_photo"] is True
    assert punches[1]["has_photo"] is False
    assert all(isinstance(p["punch_id"], int) for p in punches)
    assert punches[0]["punched_at"].startswith("2026-07-07T09:00")


def test_detail_returns_hours_and_warnings(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get(f"/api/timecards/{card_id}",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "open"
    assert body["total_minutes"] == 480
    day = body["days"][0]
    assert day["worked_minutes"] == 480
    assert "no_meal_break" in day["warnings"]  # 8h with no lunch punched


def test_list_returns_cards_in_scope(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/timecards",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    assert [card["timecard_id"] for card in r.json()] == [card_id]
    assert r.json()[0]["total_minutes"] == 480


def test_list_hides_cards_outside_the_callers_property(db_engine, db_session, tmp_path):
    _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    r = c.get("/api/timecards", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json() == []  # out-of-scope cards are invisible, not merely un-approvable


def test_unauthenticated_is_401(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, _mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    assert c.get(f"/api/timecards/{card_id}").status_code == 401


def test_accountant_cannot_see_timecards(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get(f"/api/timecards/{card_id}",
              headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 403


def test_accountant_cannot_approve(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.post(f"/api/timecards/{card_id}/approve",
               headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 403
    card = db_session.get(Timecard, card_id)
    db_session.refresh(card)
    assert card.status == "open"


def test_department_manager_cannot_approve(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["department_manager"], sub="dm",
               scopes=[{"property_id": "HISJ", "department_id": 1}])
    assert c.post(f"/api/timecards/{card_id}/approve",
                  headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_gm_confined_to_own_property(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "SSSJ", "department_id": None}])  # wrong property
    r = c.get(f"/api/timecards/{card_id}", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


def test_gm_in_own_property_can_approve(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "HISJ", "department_id": None}])
    r = c.post(f"/api/timecards/{card_id}/approve",
               headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    card = db_session.get(Timecard, card_id)
    db_session.refresh(card)
    assert card.approved_by == "gm"
    assert card.approved_at is not None


def test_out_of_scope_gm_cannot_approve_and_card_stays_open(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    assert c.post(f"/api/timecards/{card_id}/approve",
                  headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    card = db_session.get(Timecard, card_id)
    db_session.refresh(card)
    assert card.status == "open"
    assert card.approved_by is None


def test_co_held_global_role_does_not_widen_approval_scope(db_engine, db_session, tmp_path):
    """The A2.3 rule: approval confinement uses `assignment_scope`, NOT
    `resolve_scope`. An accountant is a GLOBAL-property VIEW role — co-holding it
    must not let a GM approve a property they are not assigned to."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["accountant", "property_gm"], sub="gm",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    hdr = {"Authorization": f"Bearer {tok}"}
    assert c.get(f"/api/timecards/{card_id}", headers=hdr).status_code == 403
    assert c.post(f"/api/timecards/{card_id}/approve", headers=hdr).status_code == 403
    assert c.get("/api/timecards", headers=hdr).json() == []
    card = db_session.get(Timecard, card_id)
    db_session.refresh(card)
    assert card.status == "open"


def test_gm_with_no_assignment_at_all_is_denied(db_engine, db_session, tmp_path):
    """Fail closed: no `scopes` claim and no role_assignment row = no properties."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    hdr = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='rogue')}"}
    assert c.get(f"/api/timecards/{card_id}", headers=hdr).status_code == 403
    assert c.post(f"/api/timecards/{card_id}/approve", headers=hdr).status_code == 403


def test_department_scoped_assignment_is_not_property_wide(db_engine, db_session, tmp_path):
    """A department-scoped GM assignment does not confer property-wide approval
    (Scope.allows_property deliberately ignores `departments`)."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "HISJ", "department_id": 1}])
    assert c.post(f"/api/timecards/{card_id}/approve",
                  headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_unknown_timecard_is_404(db_engine, db_session, tmp_path):
    _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    assert c.get("/api/timecards/99999", headers=admin).status_code == 404
    assert c.post("/api/timecards/99999/approve", headers=admin).status_code == 404


def test_approve_locks_the_card(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    r = c.post(f"/api/timecards/{card_id}/approve", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    # And re-approving is refused.
    assert c.post(f"/api/timecards/{card_id}/approve", headers=admin).status_code == 409

    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "approve_timecard")
    ).scalars().all()
    assert len(audits) == 1  # the refused re-approval writes no audit row
    assert audits[0].actor_subject == "adm"
    assert audits[0].resource_id == str(card_id)


def test_approving_a_card_promotes_labor_facts(db_engine, db_session, tmp_path):
    """The B3 payoff: approving a card promotes its approved hours into
    department-level labor facts. The seeded employee has no department/pay_rate,
    so set both before approving to produce a COSTED fact."""
    device_id, emp_id = _seed(db_session)
    # Give the employee a department and an hourly rate so the promote costs it.
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    emp = db_session.get(Employee, emp_id)
    assert emp is not None
    set_department(db_session, emp, dept.department_id)
    set_rate_everywhere(db_session, emp, "20.00")
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    _punch(db_session, device_id, emp_id, "clock_out", 17)  # 8h day
    db_session.commit()
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    r = c.post(f"/api/timecards/{card.timecard_id}/approve", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    facts = db_session.execute(
        select(UsaliLaborFact).where(UsaliLaborFact.timecard_id == card.timecard_id)
    ).scalars().all()
    assert len(facts) == 1
    assert facts[0].department_id == dept.department_id
    assert facts[0].est_cost > 0  # 8h × $20 = $160


def test_approved_card_refuses_further_adjustments(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    assert c.post(f"/api/timecards/{card_id}/approve", headers=admin).status_code == 200

    # Locked: a further adjustment is refused.
    adj = c.post(f"/api/timecards/{card_id}/adjustments", headers=admin, json={
        "punch_type": "clock_out", "adjusted_at": "2026-07-07T17:00:00Z",
        "business_date": "2026-07-07", "reason": "late fix",
    })
    assert adj.status_code == 409
    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []


# --- Task 6: audited additive adjustments ---------------------------------


def _adjustment(**overrides):
    body = {
        "punch_type": "clock_out", "adjusted_at": "2026-07-07T17:00:00Z",
        "business_date": "2026-07-07", "reason": "forgot to clock out",
    }
    body.update(overrides)
    return body


def test_adjustment_fixes_a_missing_clock_out_without_mutating_punches(
    db_engine, db_session, tmp_path
):
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9)  # no clock_out
    db_session.commit()
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    before = c.get(f"/api/timecards/{card.timecard_id}", headers=admin).json()
    assert "missing_clock_out" in before["days"][0]["warnings"]
    assert before["total_minutes"] == 0

    r = c.post(f"/api/timecards/{card.timecard_id}/adjustments", headers=admin,
               json=_adjustment())
    assert r.status_code == 201
    # The response is the recomputed card — the manager sees the fix immediately.
    assert r.json()["total_minutes"] == 480

    after = c.get(f"/api/timecards/{card.timecard_id}", headers=admin).json()
    assert "missing_clock_out" not in after["days"][0]["warnings"]
    assert after["total_minutes"] == 480

    # Punches are IMMUTABLE — the fix is an additive adjustment row, not an edit.
    punches = db_session.execute(select(Punch)).scalars().all()
    assert len(punches) == 1 and punches[0].punch_type == "clock_in"
    assert punches[0].punched_at == datetime(2026, 7, 7, 9, tzinfo=UTC)
    adjustments = db_session.execute(select(TimecardAdjustment)).scalars().all()
    assert len(adjustments) == 1
    assert adjustments[0].actor_subject == "adm"
    assert adjustments[0].reason == "forgot to clock out"
    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "adjust_timecard")
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].actor_subject == "adm"
    assert audits[0].resource_id == str(card.timecard_id)


def test_accountant_cannot_adjust(db_engine, db_session, tmp_path):
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.post(f"/api/timecards/{card_id}/adjustments",
               headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"},
               json=_adjustment())
    assert r.status_code == 403
    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []


def test_out_of_scope_gm_cannot_adjust_another_propertys_card(
    db_engine, db_session, tmp_path
):
    """Scope confinement: an adjustment is a write on someone else's hours."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "SSSJ", "department_id": None}])
    r = c.post(f"/api/timecards/{card_id}/adjustments",
               headers={"Authorization": f"Bearer {tok}"}, json=_adjustment())
    assert r.status_code == 403
    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []
    assert db_session.execute(select(AuditEvent)).scalars().all() == []


def test_adjustment_on_unknown_card_is_404(db_engine, db_session, tmp_path):
    _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    r = c.post("/api/timecards/99999/adjustments", headers=admin, json=_adjustment())
    assert r.status_code == 404


def test_unknown_punch_type_is_rejected(db_engine, db_session, tmp_path):
    """The engine silently IGNORES an event type it does not know — so an
    unvalidated `punch_type` would persist a row that pays nothing and explains
    nothing. Reject it at the door."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    r = c.post(f"/api/timecards/{card_id}/adjustments", headers=admin,
               json=_adjustment(punch_type="teleport"))
    assert r.status_code == 422
    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []


def test_adjustment_outside_the_cards_period_is_rejected(db_engine, db_session, tmp_path):
    """An adjustment is keyed to the card by FK but grouped by `business_date`.
    A date outside the period would mint a phantom day on this card and book
    hours into a period they did not happen in — refuse it."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    for bad in ("2026-06-01", "2026-08-20"):
        r = c.post(f"/api/timecards/{card_id}/adjustments", headers=admin,
                   json=_adjustment(business_date=bad,
                                    adjusted_at=f"{bad}T17:00:00Z"))
        assert r.status_code == 422, bad

    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []
    body = c.get(f"/api/timecards/{card_id}", headers=admin).json()
    assert len(body["days"]) == 1  # no phantom day, hours unchanged
    assert body["total_minutes"] == 480


def test_adjusted_at_outside_the_cards_period_is_rejected(db_engine, db_session, tmp_path):
    """An in-period business_date with a typo'd adjusted_at (wrong year) would
    book arbitrary hours. Bound adjusted_at's DATE to the same inclusive period."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    r = c.post(f"/api/timecards/{card_id}/adjustments", headers=admin,
               json=_adjustment(business_date="2026-07-07",
                                adjusted_at="2025-07-07T17:00:00Z"))
    assert r.status_code == 422
    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []
    assert db_session.execute(select(AuditEvent)).scalars().all() == []


def test_naive_adjusted_at_is_rejected(db_engine, db_session, tmp_path):
    """Punches are tz-aware. A naive `adjusted_at` merged into the same timeline
    blows up the sort (naive vs aware) — and guessing a zone would silently move
    someone's hours. Demand an offset."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}
    r = c.post(f"/api/timecards/{card_id}/adjustments", headers=admin,
               json=_adjustment(adjusted_at="2026-07-07T17:00:00"))
    assert r.status_code == 422
    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []


def test_reason_is_required_and_bounded(db_engine, db_session, tmp_path):
    """`reason` is the whole audit value of an adjustment — and the column is
    String(300), so an unbounded one is a 500 waiting to happen."""
    card_id, _ = _open_card(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    assert c.post(f"/api/timecards/{card_id}/adjustments", headers=admin,
                  json=_adjustment(reason="")).status_code == 422
    assert c.post(f"/api/timecards/{card_id}/adjustments", headers=admin,
                  json=_adjustment(reason="x" * 301)).status_code == 422
    assert db_session.execute(select(TimecardAdjustment)).scalars().all() == []


def test_adjustments_accumulate_and_are_all_audited(db_engine, db_session, tmp_path):
    """Corrections are additive: a second adjustment never overwrites the first,
    and the trail keeps both."""
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    db_session.commit()
    card = assemble_timecard(db_session, emp_id, date(2026, 7, 7), anchor=_ANCHOR)
    db_session.commit()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    admin = {"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"}

    assert c.post(f"/api/timecards/{card.timecard_id}/adjustments", headers=admin,
                  json=_adjustment(punch_type="lunch_start",
                                   adjusted_at="2026-07-07T12:00:00Z",
                                   reason="meal not punched")).status_code == 201
    assert c.post(f"/api/timecards/{card.timecard_id}/adjustments", headers=admin,
                  json=_adjustment(punch_type="lunch_end",
                                   adjusted_at="2026-07-07T12:30:00Z",
                                   reason="meal not punched")).status_code == 201
    r = c.post(f"/api/timecards/{card.timecard_id}/adjustments", headers=admin,
               json=_adjustment())
    assert r.status_code == 201
    assert r.json()["total_minutes"] == 450  # 8h shift less the 30m unpaid meal
    assert r.json()["days"][0]["warnings"] == []

    assert len(db_session.execute(select(TimecardAdjustment)).scalars().all()) == 3
    audits = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "adjust_timecard")
    ).scalars().all()
    assert len(audits) == 3


# --- Task 7: the gated, audited photo read --------------------------------
#
# This is the platform's FIRST photo-read surface: B1 shipped with zero photo
# egress by design, and the object here is an employee's face. Every test below
# is a gate on PII leaving the system.


def _one_punch(db_session):
    device_id, emp_id = _seed(db_session)
    _punch(db_session, device_id, emp_id, "clock_in", 9)
    db_session.commit()
    return db_session.execute(select(Punch)).scalars().one()


def _photo_audits(db_session):
    return db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "view_punch_photo")
    ).scalars().all()


def test_photo_read_is_gated_scoped_and_audited(db_engine, db_session, tmp_path):
    punch = _one_punch(db_session)

    store = InMemoryPhotoStore()
    store.put(punch.photo_key, b"\xff\xd8\xff jpeg-bytes")

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, store)

    # An accountant (no approval authority) is refused.
    denied = c.get(f"/api/punches/{punch.punch_id}/photo",
                   headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert denied.status_code == 403

    # A GM scoped to ANOTHER property is refused.
    other = mint(roles=["property_gm"], sub="gm2",
                 scopes=[{"property_id": "SSSJ", "department_id": None}])
    assert c.get(f"/api/punches/{punch.punch_id}/photo",
                 headers={"Authorization": f"Bearer {other}"}).status_code == 403

    # The org_admin gets the decrypted image, uncacheable, and it is AUDITED.
    r = c.get(f"/api/punches/{punch.punch_id}/photo",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff jpeg-bytes"
    assert r.headers["content-type"] == "image/jpeg"
    assert "no-store" in r.headers["cache-control"]

    audits = _photo_audits(db_session)
    assert len(audits) == 1
    assert audits[0].actor_subject == "adm"
    assert audits[0].resource_id == str(punch.punch_id)


def test_purged_photo_is_404(db_engine, db_session, tmp_path):
    punch = _one_punch(db_session)

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, InMemoryPhotoStore())  # store is empty
    r = c.get(f"/api/punches/{punch.punch_id}/photo",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 404
    # No bytes left the system, so there is nothing to have viewed: the audit
    # trail records EGRESS, and a 404 is not egress.
    assert _photo_audits(db_session) == []


def test_gm_in_own_property_can_read_the_photo(db_engine, db_session, tmp_path):
    """The whole point of the endpoint: the approving GM verifies the face."""
    punch = _one_punch(db_session)
    store = InMemoryPhotoStore()
    store.put(punch.photo_key, b"\xff\xd8\xff jpeg-bytes")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, store)
    tok = mint(roles=["property_gm"], sub="gm",
               scopes=[{"property_id": "HISJ", "department_id": None}])
    r = c.get(f"/api/punches/{punch.punch_id}/photo",
              headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff jpeg-bytes"
    assert [a.actor_subject for a in _photo_audits(db_session)] == ["gm"]


def test_unauthenticated_photo_read_is_401(db_engine, db_session, tmp_path):
    punch = _one_punch(db_session)
    verifier, _mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    assert c.get(f"/api/punches/{punch.punch_id}/photo").status_code == 401
    assert _photo_audits(db_session) == []


def test_unknown_punch_photo_is_404(db_engine, db_session, tmp_path):
    _one_punch(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get("/api/punches/99999/photo",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 404
    assert _photo_audits(db_session) == []


def test_punch_with_no_photo_key_is_404(db_engine, db_session, tmp_path):
    """`photo_key` is nullable (and the purge job NULLs it). A punch whose photo
    was never stored — or has already been purged — has nothing to serve."""
    device_id, emp_id = _seed(db_session)
    db_session.add(Punch(
        employee_id=emp_id, kiosk_device_id=device_id, punch_type="clock_in",
        punched_at=datetime(2026, 7, 7, 9, tzinfo=UTC),
        business_date=date(2026, 7, 7), photo_key=None,
    ))
    db_session.commit()
    punch = db_session.execute(select(Punch)).scalars().one()

    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    r = c.get(f"/api/punches/{punch.punch_id}/photo",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 404
    assert _photo_audits(db_session) == []


def test_denied_photo_reads_return_no_bytes_and_no_identifiers(
    db_engine, db_session, tmp_path
):
    """A refusal must not become a side channel: no image bytes, and a body that
    names neither the employee nor the property it belongs to."""
    punch = _one_punch(db_session)
    store = InMemoryPhotoStore()
    store.put(punch.photo_key, b"\xff\xd8\xff jpeg-bytes")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, store)

    tokens = [
        mint(roles=["accountant"], sub="acct"),
        mint(roles=["department_manager"], sub="dm",
             scopes=[{"property_id": "HISJ", "department_id": 1}]),
        mint(roles=["property_gm"], sub="gm2",
             scopes=[{"property_id": "SSSJ", "department_id": None}]),
        mint(roles=["property_gm"], sub="rogue"),  # no assignment at all
        mint(roles=["accountant", "property_gm"], sub="gm3",  # the A2.3 rule
             scopes=[{"property_id": "SSSJ", "department_id": None}]),
    ]
    for tok in tokens:
        r = c.get(f"/api/punches/{punch.punch_id}/photo",
                  headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 403, r.text
        assert b"\xff\xd8\xff" not in r.content
        assert "Hank" not in r.text and "HISJ" not in r.text
        assert punch.photo_key not in r.text

    # Denials are refusals of egress, not egress: nothing to audit as "viewed".
    assert _photo_audits(db_session) == []


def test_photo_response_leaks_nothing_in_its_headers(db_engine, db_session, tmp_path):
    """The photo key encodes property + business date, and the employee has a
    name. Neither may ride out on a header — and the browser must not be allowed
    to sniff the body into something executable."""
    punch = _one_punch(db_session)
    store = InMemoryPhotoStore()
    store.put(punch.photo_key, b"\xff\xd8\xff jpeg-bytes")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, store)

    r = c.get(f"/api/punches/{punch.punch_id}/photo",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    blob = " ".join(f"{k}: {v}" for k, v in r.headers.items())
    for secret in (punch.photo_key, "Hank", "HISJ", "2026-07-07"):
        assert secret not in blob


def test_non_jpeg_bytes_are_never_served_as_an_image(db_engine, db_session, tmp_path):
    """The kiosk enforces the JPEG magic on upload, so this can only happen if a
    blob was written out-of-band — but the store is not the authority on what it
    holds. Serve an unrecognised blob as an inert download, never as content the
    browser will render (and so never as stored XSS on our own origin)."""
    punch = _one_punch(db_session)
    store = InMemoryPhotoStore()
    store.put(punch.photo_key, b"<html><script>alert(document.cookie)</script>")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, store)

    r = c.get(f"/api/punches/{punch.punch_id}/photo",
              headers={"Authorization": f"Bearer {mint(roles=['org_admin'], sub='adm')}"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == "attachment"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in r.headers["cache-control"]
    # Still a read of the employee's punch evidence — still audited.
    assert len(_photo_audits(db_session)) == 1
