"""H9 adversarial-review remediation — the findings that got fixes.

Money Critical: reopen RELINKS the orphan punch, and H4's population is
unlinked punches only — so the sanctioned resolution for a late punch
silently retired the only guard naming it, before any re-approval and
forever after. Fix: `_paid_worked_problems`, the worked-hours twin of
H6's content guard — stored `PayRunLine.hours - sick_hours` vs the
card's current derivation, per employee, for every submitted period.
It also names a paid period whose card sits reopened-and-never-
re-approved (the facts were demoted; the books are short with nothing
saying so).

PII High: a photo-bearing punch relinked onto a purge-STAMPED card was
unreachable by every purge path — `photos_purged_at` filtered the card
path, the link filtered the orphan path. Fix: reopen clears the stamp;
the retention clock genuinely restarts at re-approval.

PII/money convergence: H4 attributed orphans to the KIOSK's property,
but reopen authority and the paycheck both live with the PRIMARY
property — the blocked operator could not resolve, the paying run
submitted clean. Fix: route per-day primary (the sick guard's own
attribution), kiosk property as the fallback when no primary resolves.

Plus the migration lens's surviving-mutant pins: the H4 boundary day,
the approve endpoint's cutoff wiring, per-field fingerprint coverage,
the NUL separators, and the orphan-purge fence.
"""

from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, update as sql_update

from tests.authkit import make_authkit
from tests.employees import make_employee, place
from tests.test_e5_provider_port import _chain_row, _payable_employee
from tests.test_g7_review_remediation import _execute
from tests.test_payroll_run import _approved_card, _seed, _shift
from tests.test_sick_pay_submission import _sick
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    Department,
    KioskDevice,
    Property,
    ProviderEmployeeRef,
    Punch,
    Timecard,
)
from usali.payroll_provider import InMemoryPayrollProvider
from usali.payroll_run import (
    assemble_pay_run_entries,
    payload_fingerprint,
    provider_payload_stale,
)
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.timecards import assemble_timecard, purge_approved_photos

_ANCHOR = date(2026, 1, 5)
_PERIOD_DAY = date(2026, 7, 6)      # period 2026-07-06 .. 2026-07-19
_NEXT_PERIOD_DAY = date(2026, 7, 20)
_WORKED_MARK = "pay run paid"


def _next_preflight(db_session, provider):
    return assemble_pay_run_entries(
        db_session, "HISJ", _NEXT_PERIOD_DAY, anchor=_ANCHOR,
        provider_capabilities=provider.capabilities(),
    )


def _late_punch(db_session, emp_id, device_id, day, *, hours=(17, 21),
                photo_key=None):
    for i, (ptype, hour) in enumerate(
        (("clock_in", hours[0]), ("clock_out", hours[1]))
    ):
        db_session.add(Punch(
            employee_id=emp_id, kiosk_device_id=device_id, punch_type=ptype,
            punched_at=datetime(day.year, day.month, day.day, hour,
                                tzinfo=UTC),
            business_date=day,
            photo_key=(photo_key if i == 0 else None),
        ))
    db_session.flush()


def _reopen_directly(db_session, emp_id, in_period):
    """The reopen endpoint's card mutations, minus HTTP: status flip,
    identity cleared, relink. (Fact demotion is irrelevant to the guard —
    it reads cards and lines.)"""
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == emp_id,
                               Timecard.period_start == _PERIOD_DAY)
    ).scalar_one()
    card.status = "open"
    card.approved_by = None
    card.approved_at = None
    assemble_timecard(db_session, emp_id, in_period, anchor=_ANCHOR)
    db_session.commit()
    return card


def _worked_lines(report):
    return [p for p in report.problems if _WORKED_MARK in p]


# --- the money Critical: relinked minutes must stay named --------------------


def test_relinked_minutes_after_reopen_stay_named(db_session):
    """The review's Critical: run pays 8h; a 4h punch lands late (H4 names
    it — good); the GM presses the sanctioned Reopen. The relink retires
    H4's marker, so before this fix the very act of resolving made the
    minutes silently unpaid forever. Now: the reopened-unapproved state
    is named, and after re-approval the content comparison (card 12h vs
    paid 8h) keeps naming the 4h the run provably did not pay."""
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

    # Stage 1: the orphan marker (H4) names it.
    report = _next_preflight(db_session, provider)
    assert any("linked to no timecard" in p for p in report.problems)

    # Stage 2: reopen relinks — the marker is gone, the content guard
    # takes over (reopened paid period, not yet re-approved).
    _reopen_directly(db_session, hank, _PERIOD_DAY)
    report = _next_preflight(db_session, provider)
    named = [p for p in report.problems
             if "Hank H" in p and _PERIOD_DAY.isoformat() in p]
    assert named, report.problems

    # Stage 3: re-approved — books right, money still short: 12h on the
    # card, 8h paid. The comparison names the exact figures.
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == hank,
                               Timecard.period_start == _PERIOD_DAY)
    ).scalar_one()
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    report = _next_preflight(db_session, provider)
    [line] = _worked_lines(report)
    assert "Hank H" in line
    assert "12.00" in line and "8.00" in line
    assert not report.ok


def test_a_reopened_card_never_reapproved_is_named(db_session):
    """The abandoned-reopen shape: facts demoted at reopen, nobody
    re-approves, and before this fix nothing anywhere named the open
    card — Schedule 14/15 silently short by the period's labor cost."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason
    _reopen_directly(db_session, hank, _PERIOD_DAY)

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    report = _next_preflight(db_session, provider)
    named = [p for p in report.problems
             if "Hank H" in p and "re-approve" in p]
    assert named, report.problems


def test_a_settled_period_with_sick_stays_silent_in_both_channels(
    db_session, caplog,
):
    """The over-blocking direction, with sick in the mix: stored hours
    include the sick share, so the worked comparison must subtract it —
    a guard that compared card-worked 8h against stored 16h would
    false-alarm every settled sick period."""
    import logging

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _sick(db_session, hank, "8.00", date(2026, 7, 8))
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)

    logging.getLogger("usali.payroll_run").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.payroll_run"):
        report = _next_preflight(db_session, provider)
    assert report.ok, report.problems
    # A settled period is silent in BOTH channels: any warning here means
    # a comparison direction is wrong (e.g. sick not subtracted reads as
    # "card claims less than paid" and mutters forever).
    assert not caplog.records, [r.getMessage() for r in caplog.records]


def test_fractional_minutes_settle_exactly(db_session):
    """The standing whole-dollar rule, applied to hours: a 6h17m shift
    (6.28h after the per-day cents quantization) must round-trip through
    submit and the content comparison without a phantom cent of drift —
    and a genuine 1h relink must then surface as exactly 1.00h."""
    from tests.test_e5_provider_port import _ssn_profile

    dept_id, pos_id, device_id = _seed(db_session)
    frac = make_employee(db_session, property_id="HISJ",
                         department_id=dept_id, position_id=pos_id,
                         full_name="Frac F", pay_type="hourly",
                         pay_rate="19.37")
    db_session.flush()
    hank = frac.employee_id
    _ssn_profile(db_session, hank)
    _chain_row(db_session, hank, 1)
    # One 6h17m shift (17:00 - 23:17): 377 minutes -> 6.28h after the
    # per-day cents quantization.
    db_session.add(Punch(
        employee_id=hank, kiosk_device_id=device_id, punch_type="clock_in",
        punched_at=datetime(2026, 7, 7, 17, 0, tzinfo=UTC),
        business_date=date(2026, 7, 7),
    ))
    db_session.add(Punch(
        employee_id=hank, kiosk_device_id=device_id, punch_type="clock_out",
        punched_at=datetime(2026, 7, 7, 23, 17, tzinfo=UTC),
        business_date=date(2026, 7, 7),
    ))
    db_session.commit()
    assemble_timecard(db_session, hank, date(2026, 7, 7), anchor=_ANCHOR)
    _approved_card(db_session, hank)
    provider = InMemoryPayrollProvider()
    run = _execute(db_session, provider)
    assert run.status == "submitted", run.failure_reason

    _shift(db_session, device_id, hank, 20, 8, 16)
    db_session.commit()
    _approved_card(db_session, hank, period_day=_NEXT_PERIOD_DAY)
    report = _next_preflight(db_session, provider)
    assert report.ok, report.problems  # settled: no phantom drift

    _late_punch(db_session, hank, device_id, date(2026, 7, 9),
                hours=(18, 19))
    _reopen_directly(db_session, hank, _PERIOD_DAY)
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == hank,
                               Timecard.period_start == _PERIOD_DAY)
    ).scalar_one()
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    report = _next_preflight(db_session, provider)
    [line] = _worked_lines(report)
    assert "7.28" in line and "6.28" in line


# --- the PII High: reopen restarts the purge clock ---------------------------


def _client(db_engine, tmp_path, verifier, store):
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=store,
    )
    return TestClient(app)


def test_reopen_restarts_the_purge_clock_for_relinked_photos(
    db_engine, db_session, tmp_path,
):
    """The review's PII High: photo purged, card STAMPED; a photo-bearing
    late punch relinks via reopen; before this fix the stamp barred the
    card path and the link barred the orphan path — the face image
    outlived retention forever. Reopen now clears the stamp, so the
    relinked photo purges once the re-approval ages past retention."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == hank)
    ).scalar_one()
    db_session.execute(
        sql_update(Timecard).where(Timecard.timecard_id == card.timecard_id)
        .values(approved_at=datetime.now(UTC) - timedelta(days=100))
    )
    db_session.commit()
    store = InMemoryPhotoStore()
    for p in db_session.execute(
        select(Punch).where(Punch.employee_id == hank)
    ).scalars():
        if p.photo_key:
            store.put(p.photo_key, b"\xff\xd8\xff x")
    purge_approved_photos(db_session, store, retention_days=90)
    db_session.commit()
    db_session.refresh(card)
    assert card.photos_purged_at is not None  # stamped

    _late_punch(db_session, hank, device_id, date(2026, 7, 9),
                photo_key="k/relinked-face")
    store.put("k/relinked-face", b"\xff\xd8\xff y")
    assemble_timecard(db_session, hank, date(2026, 7, 9), anchor=_ANCHOR)
    db_session.commit()

    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier, store)
    tok = mint(roles=["org_admin"], sub="adm")
    r = client.post(f"/api/timecards/{card.timecard_id}/reopen",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/timecards/{card.timecard_id}/approve",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text

    # Retention passes after the re-approval; the photo must purge.
    db_session.expire_all()
    db_session.execute(
        sql_update(Timecard).where(Timecard.timecard_id == card.timecard_id)
        .values(approved_at=datetime.now(UTC) - timedelta(days=100))
    )
    db_session.commit()
    purged = purge_approved_photos(db_session, store, retention_days=90)
    db_session.commit()
    assert purged == 1
    assert "k/relinked-face" not in store.keys()


# --- H4 attribution: the paying property gets the blocker --------------------


def test_an_away_workers_orphan_blocks_the_paying_property(db_session):
    """PII/money convergence: reopen authority and the paycheck live with
    the PRIMARY property, so that is where the orphan must block — the
    kiosk's property can neither reopen the card nor pay the minutes.
    Wanda: primary HISJ, orphan punched at SSSJ's kiosk. HISJ's preflight
    names it; SSSJ's stays out of it (mirrors the per-day attribution the
    sick guard has always used)."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    sssj_dept = Department(property_id="SSSJ", name="Laundry")
    db_session.add(sssj_dept)
    db_session.flush()
    sssj_device = KioskDevice(property_id="SSSJ", name="iPad2",
                              token_hash="s" * 64, enrolled_by="adm")
    db_session.add(sssj_device)
    db_session.flush()
    wanda = make_employee(db_session, property_id="HISJ",
                          department_id=dept_id, position_id=pos_id,
                          full_name="Wanda W", pay_type="hourly",
                          pay_rate="21.00")
    db_session.flush()
    place(db_session, wanda, property_id="SSSJ",
          department_id=sssj_dept.department_id, is_primary=False,
          effective_from=_ANCHOR, pay_rate="21.00")
    _shift(db_session, device_id, wanda.employee_id, 7, 9, 17)
    db_session.commit()
    _approved_card(db_session, wanda.employee_id)
    _late_punch(db_session, wanda.employee_id, sssj_device.device_id,
                date(2026, 7, 8))
    assemble_timecard(db_session, wanda.employee_id, date(2026, 7, 8),
                      anchor=_ANCHOR)
    db_session.commit()

    here = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                    anchor=_ANCHOR)
    named = [p for p in here.problems
             if "Wanda W" in p and "linked to no timecard" in p]
    assert named, here.problems
    there = assemble_pay_run_entries(db_session, "SSSJ", _PERIOD_DAY,
                                     anchor=_ANCHOR)
    assert not any("Wanda W" in p and "linked to no timecard" in p
                   for p in there.problems)


def test_an_orphan_with_no_primary_falls_back_to_the_kiosks_property(
    db_session,
):
    """The fallback leg: a punch dated where no primary placement
    resolves (here: after the assignment ended — broken data, but real
    minutes) must still be SOMEBODY's blocker. The kiosk's property
    recorded the work; it gets the name."""
    from usali.models import EmployeeAssignment

    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    assignment = db_session.execute(
        select(EmployeeAssignment).where(
            EmployeeAssignment.employee_id == hank)
    ).scalar_one()
    assignment.effective_to = date(2026, 7, 10)
    db_session.commit()
    _late_punch(db_session, hank, device_id, date(2026, 7, 15))
    assemble_timecard(db_session, hank, date(2026, 7, 15), anchor=_ANCHOR)
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    named = [p for p in report.problems
             if "linked to no timecard" in p and "2026-07-15" in p]
    assert named, report.problems


def test_an_orphan_on_the_periods_last_day_is_named_by_its_own_run(
    db_session,
):
    """The migration lens's surviving boundary mutant: `<= period_end`
    → `<` silences the single most likely orphan date — the final
    evening's correction, landing after the 4am approval boundary."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    _chain_row(db_session, hank, 1)
    _late_punch(db_session, hank, device_id, date(2026, 7, 19))
    assemble_timecard(db_session, hank, date(2026, 7, 19), anchor=_ANCHOR)
    db_session.commit()

    report = assemble_pay_run_entries(db_session, "HISJ", _PERIOD_DAY,
                                      anchor=_ANCHOR)
    named = [p for p in report.problems
             if "linked to no timecard" in p and "2026-07-19" in p]
    assert named, report.problems


# --- the approve endpoint's cutoff wiring ------------------------------------


def test_the_endpoint_respects_the_cutoff_hour_at_three_am(
    db_engine, db_session, tmp_path, monkeypatch,
):
    """Surviving mutant M5: hardcoding cutoff_hour=0 at the call site
    passed every endpoint test. At 03:00 property-local on the morning
    after period_end, the closing shift is still mid-punch: the endpoint
    — not just the predicate — must refuse."""
    dept_id, pos_id, device_id = _seed(db_session)
    hank = _payable_employee(db_session, dept_id, pos_id, device_id)
    card = db_session.execute(
        select(Timecard).where(Timecard.employee_id == hank)
    ).scalar_one()
    db_session.execute(
        sql_update(Timecard).where(Timecard.timecard_id == card.timecard_id)
        .values(status="open", approved_by=None, approved_at=None)
    )
    db_session.commit()

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            # 2026-07-20 10:00 UTC == 03:00 America/Los_Angeles (PDT):
            # before the 4am cutoff, business date is still 07-19.
            return datetime(2026, 7, 20, 10, 0, tzinfo=UTC)

    monkeypatch.setattr("usali.timecard_api.datetime", _Frozen)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier, InMemoryPhotoStore())
    tok = mint(roles=["org_admin"], sub="adm")
    r = client.post(f"/api/timecards/{card.timecard_id}/approve",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 409, r.text
    assert "in progress" in r.json()["detail"]


# --- fingerprint per-field pins ----------------------------------------------


def test_a_sealed_account_only_change_reads_stale(db_session):
    """Surviving mutant M2: every existing path re-seals routing beside
    account, so dropping `sealed_account` from the hash survived 46
    tests. Pin the field alone: one column changes, the payload is
    different, the ref must read stale."""
    from tests.test_provider_resync import _synced_world
    from usali.models import DepositAccount

    emp_id, provider, ref = _synced_world(db_session)
    db_session.execute(
        sql_update(DepositAccount)
        .where(DepositAccount.employee_id == emp_id)
        .values(sealed_account="env:changed-account-only")
    )
    db_session.commit()
    assert provider_payload_stale(db_session, emp_id, ref) is True


def test_the_fingerprint_separates_adjacent_fields(db_session):
    """Surviving mutant M3: the NUL separators were asserted in prose
    only. Boundary shift — name 'AB' + ssn 'C' vs name 'A' + ssn 'BC' —
    must produce different fingerprints, or a coordinated edit could
    read as no edit."""
    from tests.test_provider_resync import _synced_world
    from usali.models import Employee, EmployeePayrollProfile

    emp_id, _provider, _ref = _synced_world(db_session)

    def _shape(name, ssn):
        db_session.execute(sql_update(Employee)
                           .where(Employee.employee_id == emp_id)
                           .values(full_name=name))
        db_session.execute(sql_update(EmployeePayrollProfile)
                           .where(EmployeePayrollProfile.employee_id == emp_id)
                           .values(ssn_sealed=ssn))
        db_session.commit()
        return payload_fingerprint(db_session, emp_id)

    assert _shape("AB", "C") != _shape("A", "BC")


# --- the orphan-purge fence --------------------------------------------------


def test_an_old_linked_punch_on_an_open_card_never_purges(db_session):
    """Surviving mutant M4: dropping the orphan clause's `timecard_id IS
    NULL` made raw age delete LINKED punches' photos — evidence on open
    (disputed, unreviewed) cards, destroyed irreversibly. Fence pinned:
    a 100-day-old punch on an OPEN card keeps its photo."""
    dept_id, pos_id, device_id = _seed(db_session)
    emp = make_employee(db_session, property_id="HISJ",
                        department_id=dept_id, position_id=pos_id,
                        full_name="Olda O", pay_type="hourly",
                        pay_rate="20.00")
    db_session.flush()
    old_day = date(2026, 4, 20)
    db_session.add(Punch(
        employee_id=emp.employee_id, kiosk_device_id=device_id,
        punch_type="clock_in",
        punched_at=datetime.now(UTC) - timedelta(days=100),
        business_date=old_day, photo_key="HISJ/open-card-old.bin",
    ))
    db_session.flush()
    assemble_timecard(db_session, emp.employee_id, old_day, anchor=_ANCHOR)
    db_session.commit()
    store = InMemoryPhotoStore()
    store.put("HISJ/open-card-old.bin", b"\xff\xd8\xff x")

    assert purge_approved_photos(db_session, store, retention_days=90) == 0
    assert store.keys() == {"HISJ/open-card-old.bin"}


# --- the dead-clause note from the review, pinned as behavior ---------------


def test_a_null_fingerprint_is_stale_by_the_explicit_clause(db_session):
    """The review noted `is None or` is redundant (None != str is True).
    It stays: explicit NULL semantics beat incidental ones. This pin
    documents the behavior either way — a NULL ref is stale."""
    from tests.test_provider_resync import _synced_world

    emp_id, _provider, ref = _synced_world(db_session)
    db_session.execute(
        sql_update(ProviderEmployeeRef)
        .where(ProviderEmployeeRef.ref_id == ref.ref_id)
        .values(payload_fingerprint=None)
    )
    db_session.commit()
    db_session.refresh(ref)
    assert provider_payload_stale(db_session, emp_id, ref) is True
