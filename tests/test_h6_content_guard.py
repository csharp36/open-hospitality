"""H6: the §246(n) guard goes content-based (decision 7).

For every earlier period this property already submitted, the guard
compares what the ledger NOW derives (sick_days_taken, this property's
days, per employee) against what that run's lines STORED as paid (H5):
derived > stored → blocker (hours the run provably did not pay);
derived < stored → the paid-then-voided server-side log; equal →
silence. No clocks anywhere — the created_at-vs-submitted_at comparison
retires, and with it the whole timestamp-race family: transaction-start
visibility, app-vs-DB skew, and the submit-window residual this module's
first test pins.
"""

from datetime import date


from tests.employees import make_employee
from tests.test_e5_provider_port import _chain_row, _payable_employee
from tests.test_g7_review_remediation import _execute
from tests.test_payroll_run import _approved_card, _seed, _shift
from tests.test_sick_pay_submission import _sick
from usali.models import PayRun, Property
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import assemble_pay_run_entries

_ANCHOR = date(2026, 1, 5)
_SICK_DAY = date(2026, 7, 8)      # inside period 2026-07-06 .. 2026-07-19
_NEXT_PERIOD_DAY = date(2026, 7, 20)


def _next_preflight(db_session, provider):
    return assemble_pay_run_entries(
        db_session, "HISJ", _NEXT_PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )


def test_usage_recorded_during_the_submit_window_is_named(db_session):
    """THE residual the drift re-check could not see (G7 finding B): an
    entry recorded during the provider call itself. Its created_at
    precedes submitted_at, so the timestamp guard called it paid forever
    — silently unpaid, with no window left to catch it in. Content has
    no windows: the run's stored lines provably exclude the hours, and
    the next preflight names them."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()

    real_submit = provider.submit_pay_run

    def submit_and_record(**kwargs):
        result = real_submit(**kwargs)
        _sick(db_session, hank, "8.00", _SICK_DAY)  # lands mid-window
        return result

    provider.submit_pay_run = submit_and_record  # type: ignore[method-assign]
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    report = _next_preflight(db_session, provider)
    named = [p for p in report.problems if "Hank H" in p]
    assert named and "sick" in named[0] and "2026-07-08" in named[0]
    assert not report.ok


def test_a_person_the_run_never_carried_blocks_by_name(db_session):
    """derived > stored(absent = 0): the run has NO line for them at all
    — the sick-only person a run dropped (or whose usage landed after it
    went out). Absence of a line must read as 'paid nothing', never as
    'nothing to check'."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    solo = make_employee(db_session, property_id="HISJ",
                         department_id=dept_id, position_id=pos_id,
                         full_name="Sol O", pay_type="hourly",
                         pay_rate="22.00")
    db_session.flush()
    db_session.commit()
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    _sick(db_session, solo.employee_id, "8.00", _SICK_DAY)

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    report = _next_preflight(db_session, provider)
    named = [p for p in report.problems if "Sol O" in p]
    assert named and "2026-07-08" in named[0]


def test_paid_sick_stays_silent(db_session, caplog):
    """derived == stored: the run paid exactly what the ledger says was
    taken. No blocker AND no log — a guard that mutters about settled
    periods trains people to ignore it."""
    import logging

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "8.00", _SICK_DAY)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        report = _next_preflight(db_session, provider)
    assert not any("Hank H" in p for p in report.problems)
    assert not any("Hank" in r.getMessage() or str(hank) in r.getMessage()
                   for r in caplog.records)
    assert report.ok, report.problems


def test_another_propertys_day_stays_out_of_this_guard(db_session):
    """The derivation is scoped to THIS property's days (the per-day
    primary attribution). Wanda's SSSJ sick day, recorded after HISJ's
    run went out, is SSSJ's run's problem — naming her here would hand
    the blocker to a property that cannot resolve it."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    wanda = make_employee(db_session, property_id="SSSJ",
                          full_name="Wanda W", pay_type="hourly",
                          pay_rate="21.00")
    db_session.flush()
    db_session.commit()
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    _sick(db_session, wanda.employee_id, "8.00", _SICK_DAY)

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    report = _next_preflight(db_session, provider)
    assert not any("Wanda" in p for p in report.problems)

    # The other direction (the H9 review's surviving mutant M7): SSSJ has
    # NO submitted run, so ITS preflight must not walk HISJ's run and
    # false-block Wanda against lines that never carried her — the runs
    # query is property-scoped.
    sssj = assemble_pay_run_entries(
        db_session, "SSSJ", _NEXT_PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )
    assert not any("Wanda" in p for p in sssj.problems), sssj.problems


def test_a_failed_run_is_not_a_submitted_run(db_session):
    """The guard consults periods that actually PAID (submitted_at set).
    A period whose only run failed paid nothing — its sick is the
    operator-era posture (or a replacement run's future), never a
    permanent blocker derived against lines that do not exist."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "8.00", _SICK_DAY)
    db_session.add(PayRun(
        property_id="HISJ", period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 19), check_date=date(2026, 7, 24),
        status="failed", provider="memory", created_by="t",
        failure_reason="provider 500",
    ))
    db_session.commit()

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    provider = InMemoryPayrollProvider()
    report = _next_preflight(db_session, provider)
    assert not any(
        "Hank H" in p and "2026-07-08" in p for p in report.problems
    )
