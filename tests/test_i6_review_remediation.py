"""I6: the three-lens adversarial review's findings, reproduced and pinned.

Money lens: two concurrent settlements double-recorded the same delta and
the phantom excess silently absorbed a LATER, genuinely unpaid drift (the
row is now locked while the server computes); the I4 reconciliation was a
pure hours-totals oracle, so a VOID of the paid day plus an equal-hours
NEW late day read as "moved" and silenced a genuine blocker (a void in
the window now disables reconciliation — a pure attribution move never
involves a void). Both reproduced before fixing.

Guard-dimension pins (the review's verified surviving mutants): the
settled-hours SUM is scoped to ONE employee and ONE run — dropping either
WHERE survived 60–95 tests before this module; a sick-only line (approved
card, zero worked) that later gains relinked minutes must still block; a
paid line whose card VANISHED must still be named in the log; and the I4
period identity must agree on period_end, not just period_start.

Disclosure lens: refused settlement probes (404/409) were a free,
unaudited per-employee oracle — every refusal now leaves an audit row;
the note must never appear in any response body, refusal detail, or log;
an empty note refuses; an unlisted role refuses.
"""

import logging
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select, update

from tests.employees import make_employee
from tests.test_e5_provider_port import (
    _OPENER,
    _chain_row,
    _payable_employee,
    _ssn_profile,
)
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
from tests.test_i3_settlement import (
    _NOTE,
    _api,
    _blocked_world,
    _reapprove,
    _settle_audits,
    _settlements,
)
from tests.test_i4_moved_vs_missing import (
    _RECONCILED,
    _SICK_DAY,
    _preflight,
    _retroactive_transfer,
    _two_property_paid_world,
)
from tests.test_payroll_run import _approved_card, _seed, _shift
from tests.test_sick_pay_submission import _sick
from usali.db import make_session_factory
from usali.models import AuditEvent, PayRun, PayRunLine, Property, Punch, Timecard
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import (
    SettlementRefused,
    assemble_pay_run_entries,
    execute_pay_run,
    settle_worked_hours,
)
from usali.timecards import assemble_timecard


def _refusal_audits(db_session):
    return db_session.execute(
        select(AuditEvent)
        .where(AuditEvent.action == "settle_worked_hours_refused")
        .order_by(AuditEvent.event_id)
    ).scalars().all()


# --- money lens: the two reproduced Highs ------------------------------------


def test_concurrent_settlements_cannot_double_record(db_engine, db_session):
    """The review's repro: two admins settle the same 4h delta at once.
    Without the row lock both read settled=0, both record 4.00 — and the
    phantom 4h then absorbs the NEXT genuinely unpaid drift silently.
    With the lock the second computation waits for the first commit,
    reads settled=4.00, and refuses."""
    hank, run, provider, _ = _blocked_world(db_session)
    factory = make_session_factory(db_engine)
    outcome = {}
    s_a, s_b = factory(), factory()
    try:
        run_a = s_a.get(PayRun, run.pay_run_id)
        settle_worked_hours(s_a, run_a, hank, actor="a", note="first")

        def b_side():
            try:
                run_b = s_b.get(PayRun, run.pay_run_id)
                settle_worked_hours(s_b, run_b, hank, actor="b", note="second")
                s_b.commit()
                outcome["b"] = "recorded"
            except SettlementRefused:
                s_b.rollback()
                outcome["b"] = "refused"

        t = threading.Thread(target=b_side)
        t.start()
        time.sleep(0.3)  # let B reach the lock (or, unlocked, finish)
        s_a.commit()
        t.join(timeout=15)
        assert not t.is_alive(), "second settlement never resolved"
    finally:
        s_a.close()
        s_b.close()

    rows = _settlements(db_session, run.pay_run_id)
    assert [Decimal(str(r.hours)) for r in rows] == [Decimal("4.00")], (
        [str(r.hours) for r in rows]
    )
    assert outcome["b"] == "refused"


def test_a_void_plus_equal_late_day_is_missing_not_moved(db_session, caplog):
    """The review's repro of the totals oracle: HISJ paid Hank's 7/8 sick;
    the 7/8 usage is VOIDED (clawback territory), his primary moves to
    SSSJ, and a NEW never-paid 8h day on 7/9 lands late. Period-wide 8h
    taken == 8h paid — but nothing moved: one paid day was voided and a
    different day is genuinely unpaid. A void in the window disables
    reconciliation: SSSJ must block, HISJ must log voided-after, and the
    reconciliation log must fire on neither side."""
    hank, provider = _two_property_paid_world(db_session)
    _sick(db_session, hank, "8.00", _SICK_DAY, entry_type="usage_void")
    _retroactive_transfer(db_session, hank)
    _sick(db_session, hank, "8.00", date(2026, 7, 9))

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        sssj = _preflight(db_session, "SSSJ", provider)
    named = [p for p in sssj.problems if "Hank H" in p]
    assert named and "2026-07-09" in named[0], sssj.problems
    assert not any(
        _RECONCILED in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        hisj = _preflight(db_session, "HISJ", provider)
    assert not any("Hank H" in p for p in hisj.problems), hisj.problems
    hisj_logs = [r.getMessage() for r in caplog.records]
    assert any("voided after" in m for m in hisj_logs), hisj_logs
    assert not any(_RECONCILED in m for m in hisj_logs), hisj_logs


# --- the settled-sum's two WHERE dimensions ----------------------------------


def test_a_settlement_never_credits_another_employee(
    db_engine, db_session, tmp_path,
):
    """The review's top surviving mutant: dropping the employee filter
    from the settled SUM passed 95 tests — settling Hank's 4h would have
    cleared Wanda's genuinely unpaid 4h on the same run AND under-recorded
    her own settlement. Two employees, both drifted: Hank settles, Wanda
    must still block with her full figure, then settle her own 4.00."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    wanda = _payable_employee(db_session, dept_id, pos_id, device_id,
                              name="Wanda W")
    _chain_row(db_session, wanda, 1)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    for emp in (hank, wanda):
        _late_punch(db_session, emp, device_id, date(2026, 7, 8))
        assemble_timecard(db_session, emp, date(2026, 7, 8), anchor=_ANCHOR)
        _shift(db_session, device_id, emp, 20, 8, 16)
    db_session.commit()
    for emp in (hank, wanda):
        _approved_card(db_session, emp, period_day=_NEXT_PERIOD_DAY)
        _reopen_directly(db_session, emp, _PERIOD_DAY)
        _reapprove(db_session, emp)

    c, hdr = _api(db_engine, tmp_path, provider)
    pa = hdr(["payroll_admin"], "pa")
    url = f"/api/payroll/runs/{run.pay_run_id}/settlements"
    r = c.post(url, headers=pa, json={"employee_id": hank, **_NOTE})
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["hours"]) == Decimal("4.00")

    lines = _worked_lines(_next_preflight(db_session, provider))
    wanda_lines = [p for p in lines if "Wanda W" in p]
    assert wanda_lines and "12.00" in wanda_lines[0], lines
    assert not any("Hank H" in p for p in lines), lines

    r = c.post(url, headers=pa, json={"employee_id": wanda, **_NOTE})
    assert r.status_code == 201, r.text
    assert Decimal(r.json()["hours"]) == Decimal("4.00")
    report = _next_preflight(db_session, provider)
    assert _worked_lines(report) == []
    assert report.ok, report.problems


def test_a_settlement_never_credits_another_run(
    db_engine, db_session, tmp_path,
):
    """The other WHERE dimension: dropping the run filter passed 60 tests
    — a settlement on run N would have cleared the same employee's drift
    on run N+1 forever. Settle run 1's 4h, submit run 2, drift run 2 by
    4h: the guard must name run 2's period with settled counted at 0."""
    hank, run1, provider, device_id = _blocked_world(db_session)
    c, hdr = _api(db_engine, tmp_path, provider)
    r = c.post(
        f"/api/payroll/runs/{run1.pay_run_id}/settlements",
        headers=hdr(["payroll_admin"], "pa"),
        json={"employee_id": hank, **_NOTE},
    )
    assert r.status_code == 201, r.text
    assert _next_preflight(db_session, provider).ok

    run2 = execute_pay_run(
        db_session, "HISJ", _NEXT_PERIOD_DAY, anchor=_ANCHOR,
        provider=provider, provider_name="memory", opener=_OPENER,
        actor="test",
    )
    db_session.commit()
    assert run2.status == "submitted", run2.failure_reason

    _late_punch(db_session, hank, device_id, date(2026, 7, 21))
    assemble_timecard(db_session, hank, _NEXT_PERIOD_DAY, anchor=_ANCHOR)
    db_session.commit()
    card2 = db_session.execute(
        select(Timecard).where(Timecard.employee_id == hank,
                               Timecard.period_start == _NEXT_PERIOD_DAY)
    ).scalar_one()
    card2.status = "open"
    card2.approved_by = None
    card2.approved_at = None
    assemble_timecard(db_session, hank, _NEXT_PERIOD_DAY, anchor=_ANCHOR)
    card2.status = "approved"
    card2.approved_by = "gm"
    card2.approved_at = datetime.now(UTC)
    db_session.commit()

    report = assemble_pay_run_entries(
        db_session, "HISJ", date(2026, 8, 3), anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )
    named = [p for p in report.problems
             if "Hank H" in p and "pay run paid" in p]
    assert named and "2026-07-20" in named[0], report.problems
    assert "12.00" in named[0] and "8.00" in named[0]
    # Run 1's 4h settlement is run 1's history — it must not appear here.
    assert "settled outside" not in named[0]


# --- the skip-guard's exact predicate ----------------------------------------


def test_a_sick_only_line_that_gains_worked_minutes_blocks(db_session):
    """A sick-only entrant holds an (empty, approved) card: the run paid
    8h sick, 0h worked. A worked punch later relinks through reopen —
    derived 4h vs 0h paid. The review verified that weakening the skip
    predicate (`and` → `or` on stored_worked == 0) silences exactly this
    line and passes the full suite."""
    dept_id, pos_id, device_id = _seed(db_session)
    # Like _payable_employee, but with NO worked shift — sealed under the
    # e5 opener _execute opens with.
    emp = make_employee(db_session, property_id="HISJ",
                        department_id=dept_id, position_id=pos_id,
                        full_name="Hank H", pay_type="hourly",
                        pay_rate="20.00")
    db_session.flush()
    hank = emp.employee_id
    _ssn_profile(db_session, hank)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "8.00", _SICK_DAY)
    card = assemble_timecard(db_session, hank, _PERIOD_DAY, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    _late_punch(db_session, hank, device_id, date(2026, 7, 9))
    assemble_timecard(db_session, hank, _PERIOD_DAY, anchor=_ANCHOR)
    db_session.commit()
    _reopen_directly(db_session, hank, _PERIOD_DAY)
    _reapprove(db_session, hank)

    report = _next_preflight(db_session, provider)
    named = _worked_lines(report)
    assert named and "Hank H" in named[0], report.problems
    assert "4.00" in named[0] and "0.00" in named[0]


def test_paid_worked_hours_whose_card_vanished_are_logged(
    db_session, caplog,
):
    """The contract the docstring states and no test held: a line with
    paid worked hours and NO card at all is a data defect, named against
    derived 0 (the reduced-after-payment log). Weakening the skip to
    `state == \"none\"` alone silences it."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == hank,
                               Timecard.period_start == _PERIOD_DAY)
    ).scalar_one()
    db_session.execute(
        update(Punch).where(Punch.timecard_id == card.timecard_id)
        .values(timecard_id=None)
    )
    db_session.delete(card)
    db_session.commit()

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        _next_preflight(db_session, provider)
    assert any(
        "reduced after payment" in r.getMessage()
        and str(hank) in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


# --- I4 period identity ------------------------------------------------------


def test_reconciliation_needs_the_exact_period_window(db_session, caplog):
    """`period_paid` must key on (period_start, period_end), not start
    alone: a hand-inserted submitted run sharing the start but covering a
    SHORTER window would otherwise credit its stored sick against this
    window's genuinely late hours and silence the blocker."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    _sick(db_session, hank, "8.00", _SICK_DAY)  # late for the real period

    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    weird = PayRun(
        property_id="SSSJ", period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 12), check_date=date(2026, 7, 17),
        status="submitted", provider="memory", created_by="t",
        submitted_at=datetime.now(UTC),
    )
    db_session.add(weird)
    db_session.flush()
    db_session.add(PayRunLine(
        pay_run_id=weird.pay_run_id, employee_id=hank,
        hours=Decimal("8.00"), sick_hours=Decimal("8.00"),
        gross="0", employee_taxes="0", employer_taxes="0", net="0",
    ))
    db_session.commit()

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        report = _next_preflight(db_session, provider)
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "2026-07-08" in named[0], report.problems
    assert not any(
        _RECONCILED in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


# --- disclosure lens ---------------------------------------------------------


def test_refused_settlement_probes_leave_an_audit_trail(
    db_engine, db_session, tmp_path,
):
    """The review's Medium: 404/409 refusals answered per-employee
    questions (paid on this run? drift outstanding?) with NO trail, for
    a role with no other pay-run read. Every refusal now writes an audit
    row pointing at the probed run; the success action stays separate."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    c, hdr = _api(db_engine, tmp_path, provider)
    pa = hdr(["payroll_admin"], "pa")
    url = f"/api/payroll/runs/{run.pay_run_id}/settlements"
    assert c.post(url, headers=pa,
                  json={"employee_id": hank, **_NOTE}).status_code == 409
    assert c.post(url, headers=pa,
                  json={"employee_id": 999999, **_NOTE}).status_code == 404
    assert c.post("/api/payroll/runs/999999/settlements", headers=pa,
                  json={"employee_id": hank, **_NOTE}).status_code == 404

    audits = _refusal_audits(db_session)
    assert [a.resource_id for a in audits] == [
        str(run.pay_run_id), str(run.pay_run_id), "999999",
    ]
    assert all(a.actor_subject == "pa" and a.resource_type == "pay_run"
               for a in audits)
    assert _settle_audits(db_session) == []
    assert _settlements(db_session, run.pay_run_id) == []


def test_the_note_never_leaves_the_audit_surface(
    db_engine, db_session, tmp_path, caplog,
):
    """The note is audit-surface ONLY: absent from the 201 body, absent
    from refusal details, absent from every log line the settle and the
    next preflight emit. (The review verified that adding it to any of
    those surfaces passed all prior tests.)"""
    hank, run, provider, _ = _blocked_world(db_session)
    c, hdr = _api(db_engine, tmp_path, provider)
    pa = hdr(["payroll_admin"], "pa")
    url = f"/api/payroll/runs/{run.pay_run_id}/settlements"

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.DEBUG):
        r = c.post(url, headers=pa, json={"employee_id": hank, **_NOTE})
        assert r.status_code == 201, r.text
        assert "note" not in r.json()
        r2 = c.post(url, headers=pa, json={"employee_id": hank, **_NOTE})
        assert r2.status_code == 409
        assert _NOTE["note"] not in r2.json()["detail"]
        _next_preflight(db_session, provider)
    assert not any(
        _NOTE["note"] in rec.getMessage() for rec in caplog.records
    ), [m for m in (rec.getMessage() for rec in caplog.records)
        if _NOTE["note"] in m]


def test_an_empty_note_refuses(db_engine, db_session, tmp_path):
    """An empty rationale is no rationale: the whole audit value of the
    note is WHY, and min_length pins that at the schema edge."""
    hank, run, provider, _ = _blocked_world(db_session)
    c, hdr = _api(db_engine, tmp_path, provider)
    r = c.post(
        f"/api/payroll/runs/{run.pay_run_id}/settlements",
        headers=hdr(["payroll_admin"], "pa"),
        json={"employee_id": hank, "note": ""},
    )
    assert r.status_code == 422, r.text
    assert _settlements(db_session, run.pay_run_id) == []


def test_an_unlisted_role_cannot_settle(db_engine, db_session, tmp_path):
    """The gate admits exactly the two money roles — a token carrying any
    OTHER role string refuses. (The review's role-widening mutant added
    roles the existing matrix never probed.)"""
    hank, run, provider, _ = _blocked_world(db_session)
    c, hdr = _api(db_engine, tmp_path, provider)
    r = c.post(
        f"/api/payroll/runs/{run.pay_run_id}/settlements",
        headers=hdr(["department_manager"], "dm"),
        json={"employee_id": hank, **_NOTE},
    )
    assert r.status_code == 403, r.text
    assert _settlements(db_session, run.pay_run_id) == []
