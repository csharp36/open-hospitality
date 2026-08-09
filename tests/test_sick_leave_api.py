"""E4 Task 4 + review remediation: the sick-leave surface.

The cap tests are built so the cap and the balance DIFFER (the review's F2:
a mutant comparing to the balance shipped green when the two coincided), a
usage entry sits ON Jan 1 (F5: the year-window boundary was unpinned), and
the overdraw test exercises the backdating hole (money F2).
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    Department,
    Organization,
    Property,
    SickLeaveLedger,
    Timecard,
    UsaliLaborFact,
)
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _client(db_engine, tmp_path):
    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app), mint


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Housekeeping")
    db_session.add(dept)
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ",
                        department_id=dept.department_id,
                        full_name="Sana S", pay_type="hourly")
    db_session.commit()
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "payroll_admin", sub="pa")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    return emp.employee_id


def _setup(db_engine, db_session, tmp_path):
    emp_id = _seed(db_session)
    c, mint = _client(db_engine, tmp_path)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    return c, pa, emp_id


def _adjust(db_session, emp_id, hours, on, note=None, cap=None):
    db_session.add(SickLeaveLedger(
        employee_id=emp_id, entry_type="adjustment", hours=Decimal(hours),
        cap_hours=None if cap is None else Decimal(cap),
        effective_on=on, note=note,
    ))
    db_session.commit()


def _usage(db_session, emp_id, hours, on):
    db_session.add(SickLeaveLedger(
        employee_id=emp_id, entry_type="usage", hours=Decimal(hours),
        effective_on=on,
    ))
    db_session.commit()


def _give_d_data(db_session, emp_id, on):
    """One promoted 8h day near `on`, so `day_length` has data (D=8) and the
    caps ENFORCE — with no data the cap is lawfully skipped."""
    card = Timecard(employee_id=emp_id, period_start=on, period_end=on,
                    status="approved")
    db_session.add(card)
    db_session.flush()
    db_session.add(UsaliLaborFact(
        property_id="HISJ", business_date=on, department_id=None,
        hours=Decimal("8.00"), ot_hours=Decimal("0.00"),
        est_cost=Decimal("0.0000"), timecard_id=card.timecard_id,
    ))
    db_session.commit()


def test_balance_read_is_gated_audited_and_derived(db_engine, db_session, tmp_path):
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "20.32", date(2026, 6, 1))

    r = c.get(f"/api/payroll/employees/{emp_id}/sick-leave", headers=pa)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["balance_hours"] == "20.32"
    assert body["usage_cap_hours"] == "40.00"   # display: statutory floors
    assert body["accrual_cap_hours"] == "80.00"
    assert body["usage_cap_window"] == "calendar_year"  # recorded, readable
    assert body["used_this_year_hours"] == "0.00"
    db_session.expire_all()
    reads = [a for a in db_session.execute(select(AuditEvent)).scalars()
             if a.action == "read_sick_leave_balance"]
    assert len(reads) == 1 and reads[0].resource_id == str(emp_id)


def test_every_write_surface_is_payroll_tier(db_engine, db_session, tmp_path):
    emp_id = _seed(db_session)
    c, mint = _client(db_engine, tmp_path)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}
    base = f"/api/payroll/employees/{emp_id}/sick-leave"
    assert c.get(base, headers=gm).status_code == 403
    body = {"hours": "8.00", "on": "2026-07-01"}
    assert c.post(f"{base}/usage", json=body, headers=gm).status_code == 403
    assert c.post(f"{base}/usage-voids", json=body, headers=gm).status_code == 403
    # The adjustment endpoint mints balance in either sign — it was the one
    # missing from this test (review F8).
    assert c.post(f"{base}/adjustments", json=body, headers=gm).status_code == 403


def test_usage_writes_a_negative_entry_and_returns_the_new_balance(
    db_engine, db_session, tmp_path
):
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "20.00", date(2026, 6, 1))

    r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/usage",
               json={"hours": "8.00", "on": "2026-07-01"}, headers=pa)
    assert r.status_code == 200, r.text
    assert r.json()["balance_hours"] == "12.00"
    assert r.json()["used_this_year_hours"] == "8.00"
    db_session.expire_all()
    entry = db_session.execute(
        select(SickLeaveLedger).where(SickLeaveLedger.entry_type == "usage")
    ).scalars().one()
    assert entry.hours == Decimal("-8.00")


def test_an_identical_retry_is_409_not_a_double_booking(
    db_engine, db_session, tmp_path
):
    """Money F7: punches debounce; usage now does too."""
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "20.00", date(2026, 6, 1))
    body = {"hours": "8.00", "on": "2026-07-01"}
    put = f"/api/payroll/employees/{emp_id}/sick-leave/usage"
    assert c.post(put, json=body, headers=pa).status_code == 200
    r = c.post(put, json=body, headers=pa)
    assert r.status_code == 409
    db_session.expire_all()
    usages = [e for e in db_session.execute(select(SickLeaveLedger)).scalars()
              if e.entry_type == "usage"]
    assert len(usages) == 1, "the retry must not deduct twice"


def test_backdated_usage_cannot_overdraw_a_later_date(
    db_engine, db_session, tmp_path
):
    """Money F2 through the real route: bank 30, use 25 mid-June; a 15-hour
    usage backdated to June 2 passed the old point-in-time check and left
    July at -10. Now: named 422, nothing stored."""
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "30.00", date(2026, 6, 1))
    _usage(db_session, emp_id, "-25.00", date(2026, 6, 15))
    r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/usage",
               json={"hours": "15.00", "on": "2026-06-02"}, headers=pa)
    assert r.status_code == 422
    assert "negative" in r.json()["detail"]
    db_session.expire_all()
    assert len([e for e in db_session.execute(select(SickLeaveLedger)).scalars()
                if e.entry_type == "usage"]) == 1


def test_overdraw_is_a_named_422_and_stores_nothing(db_engine, db_session, tmp_path):
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "4.00", date(2026, 6, 1))
    r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/usage",
               json={"hours": "8.00", "on": "2026-07-01"}, headers=pa)
    assert r.status_code == 422
    db_session.expire_all()
    assert not [e for e in db_session.execute(select(SickLeaveLedger)).scalars()
                if e.entry_type == "usage"]


def test_usage_without_a_placement_is_refused_by_name(
    db_engine, db_session, tmp_path
):
    """Money F4: hours taken outside employment are unattributable — they
    would vanish from every property's books. Refused at the door."""
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "20.00", date(2026, 6, 1))
    r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/usage",
               json={"hours": "8.00", "on": "2020-01-01"},  # pre-placement
               headers=pa)
    assert r.status_code == 422
    assert "placement" in r.json()["detail"]


def test_the_calendar_year_cap_refuses_where_balance_would_allow(
    db_engine, db_session, tmp_path
):
    """Review F2 + F5, rebuilt so the operands DIFFER: balance 60, cap 40
    (D=8 with real data), 20 used this year INCLUDING an entry ON Jan 1 —
    the boundary a `>=`->`>` mutant silently excluded. Request 25: the cap
    refuses (20+25>40) while the balance (60) would allow. Request 20 —
    exactly AT the cap — is lawful. Next January restarts the window."""
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _give_d_data(db_session, emp_id, date(2026, 6, 20))
    _adjust(db_session, emp_id, "80.00", date(2026, 1, 1), cap="80.00")
    _usage(db_session, emp_id, "-8.00", date(2026, 1, 1))   # Jan 1: F5 pin
    _usage(db_session, emp_id, "-12.00", date(2026, 3, 10))

    put = f"/api/payroll/employees/{emp_id}/sick-leave/usage"
    r = c.post(put, json={"hours": "25.00", "on": "2026-07-01"}, headers=pa)
    assert r.status_code == 422
    assert "cap" in r.json()["detail"]

    r = c.post(put, json={"hours": "20.00", "on": "2026-07-01"}, headers=pa)
    assert r.status_code == 200, r.text  # exactly at cap: lawful

    r = c.post(put, json={"hours": "1.00", "on": "2027-01-05"}, headers=pa)
    assert r.status_code == 200, r.text


def test_with_no_day_length_data_the_cap_is_skipped_not_floored(
    db_engine, db_session, tmp_path
):
    """Statute HIGH-2: a 10h/day worker's opening balance of 50 (their
    lawful five days) at cutover, BEFORE any promoted facts. Flooring D to 8
    denied hours above 40; skipping the cap is employee-favorable and always
    lawful (the cap is the employer's option)."""
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "50.00", date(2026, 6, 1))
    r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/usage",
               json={"hours": "50.00", "on": "2026-07-01"}, headers=pa)
    assert r.status_code == 200, r.text
    assert r.json()["balance_hours"] == "0.00"


def test_a_void_restores_cap_headroom_where_an_adjustment_cannot(
    db_engine, db_session, tmp_path
):
    """Statute HIGH-3: 32 real hours used + one mistaken 8 that was voided.
    The old counter charged the mistake against the year forever, denying
    the final lawful 8. Net use is 32; the last 8 must be granted."""
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _give_d_data(db_session, emp_id, date(2026, 6, 20))
    _adjust(db_session, emp_id, "80.00", date(2026, 1, 1), cap="80.00")
    _usage(db_session, emp_id, "-32.00", date(2026, 2, 10))
    _usage(db_session, emp_id, "-8.00", date(2026, 3, 10))  # the mistake

    base = f"/api/payroll/employees/{emp_id}/sick-leave"
    r = c.post(f"{base}/usage-voids",
               json={"hours": "8.00", "on": "2026-03-10"}, headers=pa)
    assert r.status_code == 200, r.text
    assert r.json()["used_this_year_hours"] == "32.00"

    r = c.post(f"{base}/usage", json={"hours": "8.00", "on": "2026-07-01"},
               headers=pa)
    assert r.status_code == 200, r.text  # 32 + 8 = exactly the 40 cap


def test_a_void_cannot_exceed_recorded_net_usage(db_engine, db_session, tmp_path):
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "20.00", date(2026, 6, 1))
    _usage(db_session, emp_id, "-8.00", date(2026, 6, 5))
    r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/usage-voids",
               json={"hours": "9.00", "on": "2026-06-05"}, headers=pa)
    assert r.status_code == 422


def test_nonpositive_usage_is_refused(db_engine, db_session, tmp_path):
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    _adjust(db_session, emp_id, "20.00", date(2026, 6, 1))
    for bad in ("0.00", "-4.00"):
        r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/usage",
                   json={"hours": bad, "on": "2026-07-01"}, headers=pa)
        assert r.status_code == 422


def test_adjustment_is_the_audited_opening_balance_path(
    db_engine, db_session, tmp_path
):
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    r = c.post(
        f"/api/payroll/employees/{emp_id}/sick-leave/adjustments",
        json={"hours": "20.32", "on": "2026-07-20",
              "note": "opening balance, incumbent statement 2026-07-19"},
        headers=pa,
    )
    assert r.status_code == 200, r.text
    assert r.json()["balance_hours"] == "20.32"
    db_session.expire_all()
    entry = db_session.execute(
        select(SickLeaveLedger).where(SickLeaveLedger.entry_type == "adjustment")
    ).scalars().one()
    assert entry.note is not None and "incumbent" in entry.note
    audit = [a for a in db_session.execute(select(AuditEvent)).scalars()
             if a.action == "record_sick_leave_adjustment"]
    assert len(audit) == 1 and audit[0].resource_id == str(emp_id)


def test_zero_adjustment_is_refused(db_engine, db_session, tmp_path):
    c, pa, emp_id = _setup(db_engine, db_session, tmp_path)
    r = c.post(f"/api/payroll/employees/{emp_id}/sick-leave/adjustments",
               json={"hours": "0.00", "on": "2026-07-20"}, headers=pa)
    assert r.status_code == 422


def test_unknown_employee_is_404(db_engine, db_session, tmp_path):
    c, pa, _ = _setup(db_engine, db_session, tmp_path)
    assert c.get("/api/payroll/employees/999999/sick-leave",
                 headers=pa).status_code == 404
