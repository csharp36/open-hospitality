"""I3: settlement — the explicit, authorized, audited terminal resolution.

`POST /api/payroll/runs/{id}/settlements` records that a named worked-hours
delta was paid OUTSIDE the integration. The server computes the CURRENT
delta itself (derived worked minus stored worked minus already-settled) and
records exactly that — the caller acknowledges what IS, never a figure they
typed, so a settlement can neither overshoot nor mask a later drift. Zero
delta refuses (the F6 acknowledgment rule: the trail never records an act
that had no effect). The worked-hours guard subtracts the settled sum —
blocker iff derived > stored + settled — and a post-settlement punch
re-blocks with the residual only.

Money roles only (ORG_ADMIN / PAYROLL_ADMIN), audited with a row pointing
at the settlement (which carries the delta, actor, and note). The note is
bounded operator free text, audit-surface only — never echoed into
preflight strings.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select

from tests.authkit import make_authkit
from tests.grants import grant_role
from tests.test_e5_provider_port import _chain_row, _payable_employee
from tests.test_g7_review_remediation import _execute
from tests.test_h9_review_remediation import (
    _ANCHOR,
    _NEXT_PERIOD_DAY,
    _PERIOD_DAY,
    _late_punch,
    _next_preflight,
    _reopen_directly,
    _worked_lines,
)
from tests.test_payroll_run import _approved_card, _seed, _shift
from tests.test_payroll_run_api import _client
from usali.models import AuditEvent, PayRun, Timecard, WageSettlement
from usali.payroll_provider import InMemoryPayrollProvider
from usali.timecards import assemble_timecard

_NOTE = {"note": "paid by manual check 4471"}


def _reapprove(db_session, emp_id):
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == emp_id,
                               Timecard.period_start == _PERIOD_DAY)
    ).scalar_one()
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()


def _blocked_world(db_session, *, reapprove=True):
    """The H9 Critical's shape, ready to settle: the run paid 8h worked; a
    4h punch landed late and was brought in through the sanctioned reopen
    (+ re-approve unless told otherwise). Derived 12h vs paid 8h — the
    worked guard names 4h nothing can clear until settlement exists."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    _late_punch(db_session, hank, device_id, date(2026, 7, 8))
    assemble_timecard(db_session, hank, date(2026, 7, 8), anchor=_ANCHOR)
    db_session.commit()
    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)
    _reopen_directly(db_session, hank, _PERIOD_DAY)
    if reapprove:
        _reapprove(db_session, hank)
    return hank, run, provider, device_id


def _api(db_engine, tmp_path, provider):
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)

    def hdr(roles, sub):
        return {"Authorization": f"Bearer {mint(roles=roles, sub=sub)}"}

    return c, hdr


def _settlements(db_session, run_id):
    return db_session.execute(
        select(WageSettlement).where(WageSettlement.pay_run_id == run_id)
        .order_by(WageSettlement.settlement_id)
    ).scalars().all()


def _settle_audits(db_session):
    return db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "settle_worked_hours")
    ).scalars().all()


def test_settle_clears_the_exact_blocker_and_audits(
    db_engine, db_session, tmp_path,
):
    """The full loop: the guard names 4h the run provably did not pay;
    the settlement records exactly that server-computed delta; the next
    preflight is green. The audit row points at the settlement, which
    carries the delta, the actor, and the note."""
    hank, run, provider, _ = _blocked_world(db_session)
    [line] = _worked_lines(_next_preflight(db_session, provider))
    assert "12.00" in line and "8.00" in line

    c, hdr = _api(db_engine, tmp_path, provider)
    r = c.post(
        f"/api/payroll/runs/{run.pay_run_id}/settlements",
        headers=hdr(["payroll_admin"], "pa"),
        json={"employee_id": hank, **_NOTE},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert Decimal(body["hours"]) == Decimal("4.00")
    assert body["pay_run_id"] == run.pay_run_id
    assert body["employee_id"] == hank

    [row] = _settlements(db_session, run.pay_run_id)
    assert Decimal(str(row.hours)) == Decimal("4.00")
    assert row.actor_subject == "pa"
    assert row.note == _NOTE["note"]
    [audit] = _settle_audits(db_session)
    assert audit.actor_subject == "pa"
    assert audit.resource_type == "wage_settlement"
    assert audit.resource_id == str(row.settlement_id)

    report = _next_preflight(db_session, provider)
    assert _worked_lines(report) == []
    assert report.ok, report.problems


def test_zero_delta_refuses_and_records_nothing(
    db_engine, db_session, tmp_path,
):
    """The F6 acknowledgment rule at the endpoint: a run whose card still
    derives exactly what it paid has nothing to settle — refusing keeps
    the trail free of acts that had no effect."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    c, hdr = _api(db_engine, tmp_path, provider)
    r = c.post(
        f"/api/payroll/runs/{run.pay_run_id}/settlements",
        headers=hdr(["payroll_admin"], "pa"),
        json={"employee_id": hank, **_NOTE},
    )
    assert r.status_code == 409, r.text
    assert "nothing to settle" in r.json()["detail"]
    assert _settlements(db_session, run.pay_run_id) == []
    assert _settle_audits(db_session) == []


def test_re_drift_re_blocks_with_the_residual_and_settles_again(
    db_engine, db_session, tmp_path,
):
    """Settlement must not mask a LATER drift: after settling 4h, another
    2h punch relinks. The guard re-blocks naming the residual only —
    derived 14h vs 8h paid + 4h settled — and the second settlement
    records exactly 2.00h (already-settled subtracted server-side).
    The note never echoes into preflight strings."""
    hank, run, provider, device_id = _blocked_world(db_session)
    c, hdr = _api(db_engine, tmp_path, provider)
    pa = hdr(["payroll_admin"], "pa")
    url = f"/api/payroll/runs/{run.pay_run_id}/settlements"
    assert c.post(url, headers=pa,
                  json={"employee_id": hank, **_NOTE}).status_code == 201
    assert _next_preflight(db_session, provider).ok

    _late_punch(db_session, hank, device_id, date(2026, 7, 10),
                hours=(18, 20))
    _reopen_directly(db_session, hank, _PERIOD_DAY)
    _reapprove(db_session, hank)

    [line] = _worked_lines(_next_preflight(db_session, provider))
    assert "14.00" in line              # derived now
    assert "8.00" in line               # what the run paid
    assert "4.00" in line               # what was already settled
    assert "2.00h remain unpaid" in line
    assert _NOTE["note"] not in line    # the note is audit-surface only

    r = c.post(url, headers=pa, json={"employee_id": hank, **_NOTE})
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["hours"]) == Decimal("2.00")
    rows = _settlements(db_session, run.pay_run_id)
    assert [Decimal(str(s.hours)) for s in rows] == [
        Decimal("4.00"), Decimal("2.00"),
    ]
    report = _next_preflight(db_session, provider)
    assert _worked_lines(report) == []
    assert report.ok, report.problems


def test_settlement_is_money_roles_only(db_engine, db_session, tmp_path):
    """ORG_ADMIN and PAYROLL_ADMIN — the money roles — and nobody else.
    A GM can reopen a card; settling a wage delta is a money act."""
    hank, run, provider, _ = _blocked_world(db_session)
    grant_role(db_session, "org_admin", sub="oa")  # L4: DB-backed authority
    c, hdr = _api(db_engine, tmp_path, provider)
    url = f"/api/payroll/runs/{run.pay_run_id}/settlements"
    body = {"employee_id": hank, **_NOTE}

    assert c.post(url, headers=hdr(["accountant"], "acct"),
                  json=body).status_code == 403
    assert c.post(url, headers=hdr(["property_gm"], "gm"),
                  json=body).status_code == 403
    assert c.post(url, json=body).status_code == 401
    assert _settlements(db_session, run.pay_run_id) == []

    r = c.post(url, headers=hdr(["org_admin"], "oa"), json=body)
    assert r.status_code == 201, r.text
    [row] = _settlements(db_session, run.pay_run_id)
    assert row.actor_subject == "oa"


def test_an_unapproved_card_refuses_settlement(
    db_engine, db_session, tmp_path,
):
    """A reopened-never-re-approved card is a derivation in flux: settling
    against it would freeze a delta the re-approval is about to change.
    The refusal names the resolution; the guard keeps naming the card."""
    hank, run, provider, _ = _blocked_world(db_session, reapprove=False)
    c, hdr = _api(db_engine, tmp_path, provider)
    r = c.post(
        f"/api/payroll/runs/{run.pay_run_id}/settlements",
        headers=hdr(["payroll_admin"], "pa"),
        json={"employee_id": hank, **_NOTE},
    )
    assert r.status_code == 409, r.text
    assert "re-approve" in r.json()["detail"]
    assert _settlements(db_session, run.pay_run_id) == []
    report = _next_preflight(db_session, provider)
    assert any("re-approve" in p for p in report.problems)


def test_unknown_run_or_unpaid_employee_404_and_unsubmitted_409(
    db_engine, db_session, tmp_path,
):
    hank, run, provider, _ = _blocked_world(db_session)
    c, hdr = _api(db_engine, tmp_path, provider)
    pa = hdr(["payroll_admin"], "pa")

    assert c.post("/api/payroll/runs/999999/settlements", headers=pa,
                  json={"employee_id": hank, **_NOTE}).status_code == 404
    assert c.post(
        f"/api/payroll/runs/{run.pay_run_id}/settlements", headers=pa,
        json={"employee_id": 999999, **_NOTE},
    ).status_code == 404

    # A run that never submitted paid nothing — there is no delta against
    # it to settle, whatever its lines might one day say.
    db_session.add(PayRun(
        property_id="HISJ", period_start=date(2026, 6, 8),
        period_end=date(2026, 6, 21), check_date=date(2026, 6, 26),
        status="draft", provider="memory", created_by="test",
    ))
    db_session.commit()
    draft_id = db_session.execute(
        select(PayRun.pay_run_id).where(PayRun.status == "draft")
    ).scalar_one()
    r = c.post(f"/api/payroll/runs/{draft_id}/settlements", headers=pa,
               json={"employee_id": hank, **_NOTE})
    assert r.status_code == 409, r.text
    assert _settlements(db_session, draft_id) == []


def test_the_client_cannot_dictate_the_delta(db_engine, db_session, tmp_path):
    """The acknowledgment posture, pinned at the wire: an `hours` field in
    the body is DEAD — the server records its own computation. And the
    note bound is enforced at the schema edge (422, nothing recorded)."""
    hank, run, provider, _ = _blocked_world(db_session)
    c, hdr = _api(db_engine, tmp_path, provider)
    pa = hdr(["payroll_admin"], "pa")
    url = f"/api/payroll/runs/{run.pay_run_id}/settlements"

    assert c.post(url, headers=pa, json={
        "employee_id": hank, "note": "x" * 301,
    }).status_code == 422
    assert _settlements(db_session, run.pay_run_id) == []

    r = c.post(url, headers=pa, json={
        "employee_id": hank, "hours": "999.00", **_NOTE,
    })
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["hours"]) == Decimal("4.00")
    [row] = _settlements(db_session, run.pay_run_id)
    assert Decimal(str(row.hours)) == Decimal("4.00")
