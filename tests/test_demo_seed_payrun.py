"""G6: the demo world's pay-run stories actually EXECUTE.

The demo has two properties. G6 splits the cast: the intended preflight
blockers (mid-period raise, no rate, paperwork incomplete) all live at one
property, so the OTHER — the sick department's — preflights clean and its
runs submit end to end through the REAL Gusto adapter over the in-process
mock (the demo stack's exact path). That clean property carries the two
executable stories: sick hours riding the period-2 submission (G3), and a
between-runs deposit-chain edit that re-syncs silently (G2/G4) where it
used to raise a stale-payload blocker.

These tests run the seed against a synthetic roster shaped like the real
one (two properties, one dominant department) and then DO what the demo
operator does — so a seed change that breaks the demo breaks the suite,
not the demo.
"""

import importlib.util
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from usali.deposit_accounts import account_slot, routing_slot
from usali.gusto_adapter import GustoAdapter
from usali.gusto_mock import create_mock_gusto
from usali.labor import demote_timecard
from usali.models import (
    DepositAccount,
    Employee,
    PayRun,
    ProviderEmployeeRef,
    Timecard,
)
from usali.opener import SoftwareOpener, seal_for_test
from usali.integrations import ResolvedPayroll
from usali.payroll_provider import PayrollProvider
from usali.payroll_run import (
    assemble_pay_run_entries,
    execute_pay_run,
    settle_worked_hours,
)
from usali.qbo_client import SyncASGITransport
from usali.timecards import assemble_timecard

_SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed_payrun", _SCRIPT)
assert _spec is not None and _spec.loader is not None
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)


def _worker(ref, placements, pay_type="hourly"):
    return demo_seed.DemoWorker(
        ref=ref, full_name=f"Demo Person {ref}", pay_type=pay_type,
        placements=placements,
    )


# Housekeeping at HISJ is the dominant department -> HISJ is the clean
# property; the three SSSJ people (refs 20/21/22, sorted order) become
# raise / incomplete / no-rate; ref 13 is the only non-sick HISJ person
# left, so they host the chain-edit story.
_ROSTER = [
    _worker(10, (("HISJ", "Housekeeping"),)),
    _worker(11, (("HISJ", "Housekeeping"),)),
    _worker(12, (("HISJ", "Housekeeping"),)),
    _worker(13, (("HISJ", "Front Desk"),)),
    _worker(20, (("SSSJ", "Front Desk"),)),
    _worker(21, (("SSSJ", "Breakfast"),)),
    _worker(22, (("SSSJ", "Laundry"),)),
]
_CLEAN, _BLOCKED = "HISJ", "SSSJ"
_CHAIN_STAR, _RAISE_STAR, _INCOMPLETE_STAR = (
    "Demo Person 13", "Demo Person 20", "Demo Person 21",
)
# The settle star: home cast minus the blockers, the chain star, and the
# two takers — ref 12 is what's left. Their period-0 card is PAID by the
# seed's own run before the late evening shift lands.
_LATE_STAR = "Demo Person 12"


def _gusto() -> PayrollProvider:
    return GustoAdapter(base_url="http://mock-gusto", api_token="mock",
                        company_id="mock",
                        transport=SyncASGITransport(create_mock_gusto()))


@pytest.fixture
def demo_world(db_session, monkeypatch, tmp_path):
    """The seeded demo world + the opener its sealed PII opens with."""
    opener = SoftwareOpener.generate(key_id="demo-payrun-test")
    monkeypatch.setattr(demo_seed.SoftwareOpener, "from_settings",
                        classmethod(lambda cls, settings: opener))
    # The seed submits the settle story's period-0 run itself — through
    # this seam in the demo stack (the :9300 dev mock), through the
    # in-process mock here.
    # OH-17: the seam returns adapter AND provider name together, so the fake
    # must declare which provider it is pretending to be — the seed keys its
    # ProviderEmployeeRefs on that name.
    monkeypatch.setattr(demo_seed, "_payroll_provider",
                        lambda: ResolvedPayroll("gusto", _gusto()))
    demo_seed._seed_base(db_session)
    demo_seed._seed_people(db_session, _ROSTER)
    # Only _seed_world touches REPO_ROOT (the kiosk-token file); _seed_base
    # above still needed the real one for the mapping yamls.
    monkeypatch.setattr(demo_seed, "REPO_ROOT", tmp_path)
    notes, _ = demo_seed._seed_world(db_session, _ROSTER)
    return opener, notes


def _assemble(db_session, prop, start):
    return assemble_pay_run_entries(
        db_session, prop, start, anchor=demo_seed.ANCHOR,
        provider_capabilities=_gusto().capabilities(),
    )


def _execute(db_session, opener, provider, start):
    run = execute_pay_run(
        db_session, _CLEAN, start, anchor=demo_seed.ANCHOR,
        provider=provider, provider_name="gusto", opener=opener,
        actor="demo-test",
    )
    db_session.commit()
    return run


def _reopen_and_reapprove(db_session):
    """What the Timecards page's Reopen button + approval do to the settle
    star's period-0 card: open, demote, RELINK, approve again."""
    emp = db_session.execute(
        select(Employee.employee_id).where(Employee.full_name == _LATE_STAR)
    ).scalar_one()
    card = db_session.execute(
        select(Timecard).where(
            Timecard.employee_id == emp,
            Timecard.period_start == demo_seed.PERIOD0_START,
        )
    ).scalar_one()
    card.status = "open"
    card.approved_by = None
    card.approved_at = None
    card.photos_purged_at = None
    demote_timecard(db_session, card)
    # Relink AFTER the flip: assemble skips approved cards (H1).
    assemble_timecard(db_session, emp, demo_seed.PERIOD0_START,
                      anchor=demo_seed.ANCHOR)
    card.status = "approved"
    card.approved_by = "demo-test"
    card.approved_at = datetime.now(UTC)
    db_session.commit()
    return emp


def _settle(db_session, emp):
    run0 = db_session.execute(
        select(PayRun).where(
            PayRun.property_id == _CLEAN,
            PayRun.period_start == demo_seed.PERIOD0_START,
        )
    ).scalar_one()
    settlement = settle_worked_hours(
        db_session, run0, emp, actor="demo-test",
        note="paid by manual check (demo)",
    )
    db_session.commit()
    return settlement


def _settle_loop(db_session):
    """The whole talking point, for tests of the OTHER stories: the clean
    property's runs execute only after the loop is walked."""
    _settle(db_session, _reopen_and_reapprove(db_session))


def test_the_clean_property_preflights_clean_with_sick_on_period_2(
    demo_world, db_session,
):
    _settle_loop(db_session)  # the demo script's first act
    for start in demo_seed.PERIOD_STARTS:
        report = _assemble(db_session, _CLEAN, start)
        assert report.ok, (start, report.problems)
    report = _assemble(db_session, _CLEAN, demo_seed.PERIOD_STARTS[1])
    sick = [e for e in report.entries if e.sick_hours > 0]
    assert len(sick) == 2  # the two takers, nobody else
    assert all(e.sick_hours == Decimal("8.00") for e in sick)


def test_the_blocker_property_still_tells_the_blocker_story(
    demo_world, db_session,
):
    report = _assemble(db_session, _BLOCKED, demo_seed.PERIOD_STARTS[1])
    assert not report.ok
    text = "; ".join(report.problems)
    assert _RAISE_STAR in text       # mid-period raise -> distinct rates
    assert _INCOMPLETE_STAR in text  # payroll_data_complete false
    # ...and none of it leaks into the clean property's run (covered above).


def test_run_edit_run_resyncs_and_submits(demo_world, db_session):
    """The demo script itself: run period 1, edit the chain star's deposit
    split on the Employees page, run period 2. Before G2/G4 the second run
    refused with a stale-payload blocker; now it re-syncs (synced_at
    advances) and submits — through the real adapter and mock."""
    opener, _ = demo_world
    _settle_loop(db_session)  # period 1 is gummed until the loop is walked
    provider = _gusto()
    run1 = _execute(db_session, opener, provider, demo_seed.PERIOD_STARTS[0])
    assert run1.status == "submitted", run1.failure_reason

    chain_emp = db_session.execute(
        select(Employee.employee_id).where(Employee.full_name == _CHAIN_STAR)
    ).scalar_one()
    ref = db_session.execute(
        select(ProviderEmployeeRef).where(
            ProviderEmployeeRef.employee_id == chain_emp)
    ).scalar_one()
    before = ref.synced_at

    # The between-runs edit — what the Employees page PUT does: replace the
    # chain wholesale ($100 -> $150 to checking, remainder to savings).
    db_session.execute(DepositAccount.__table__.delete().where(
        DepositAccount.employee_id == chain_emp))
    db_session.commit()
    time.sleep(0.05)  # server clocks: the new rows must be strictly newer

    def sealed(slot, plaintext):
        return seal_for_test(opener.public_key(), plaintext,
                             aad=f"{chain_emp}:{slot}".encode()).to_json()

    for ordinal, (alloc, value, acct) in enumerate(
        [("amount", Decimal("150.00"), "checking"),
         ("remainder", None, "savings")], start=1,
    ):
        db_session.add(DepositAccount(
            employee_id=chain_emp, ordinal=ordinal, allocation_type=alloc,
            allocation_value=value, account_type=acct,
            sealed_account=sealed(account_slot(ordinal, False), b"000188813"),
            sealed_routing=sealed(routing_slot(ordinal, False), b"021000021"),
            legacy_sealed=False,
        ))
    db_session.commit()

    run2 = _execute(db_session, opener, provider, demo_seed.PERIOD_STARTS[1])
    assert run2.status == "submitted", run2.failure_reason
    db_session.refresh(ref)
    assert ref.synced_at > before  # the re-sync actually happened


def test_the_talking_points_name_each_story_and_property(demo_world):
    _, notes = demo_world
    text = "\n".join(notes)
    assert _RAISE_STAR in text and _INCOMPLETE_STAR in text
    assert "Demo Person 22" in text  # no-rate star
    assert _CHAIN_STAR in text and "re-sync" in text
    # Two of the three Housekeeping people are the named takers.
    assert sum(f"Demo Person {r}" in text for r in (10, 11, 12)) >= 2
    # I5: the settle story is told — the full loop, with the endpoint.
    assert "Reopen" in text and "UNLINKED" in text
    assert _LATE_STAR in text and "settlements" in text


def test_the_settle_story_walks_end_to_end(demo_world, db_session):
    """I5: the loop the talking point sells, stage by stage. The seeded
    world holds a PAID period-0 card carrying a late unlinked shift:
    period 1's preflight names the marker (H4); reopen + re-approve
    turns it into the worked-hours drift blocker (H9's content guard);
    the settlement act clears exactly the 4h delta (I3); and the clean
    property's preflights go green."""
    report = _assemble(db_session, _CLEAN, demo_seed.PERIOD_STARTS[0])
    marker = [p for p in report.problems if _LATE_STAR in p]
    assert marker and "linked to no timecard" in marker[0], report.problems
    assert "Reopen" in marker[0]

    emp = _reopen_and_reapprove(db_session)
    report = _assemble(db_session, _CLEAN, demo_seed.PERIOD_STARTS[0])
    drift = [p for p in report.problems if _LATE_STAR in p]
    assert drift and "pay run paid" in drift[0], report.problems
    assert "record the settlement" in drift[0]

    settlement = _settle(db_session, emp)
    assert settlement.hours == Decimal("4.00")
    for start in demo_seed.PERIOD_STARTS:
        report = _assemble(db_session, _CLEAN, start)
        assert report.ok, (start, report.problems)
