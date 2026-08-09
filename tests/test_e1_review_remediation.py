"""Fences around the three Criticals a second adversarial review found.

Every one of these guarded a fix that had ALREADY been written and documented in
a comment, and shipped with no test. The mutation pass proved it: removing the
property filter, the two priced-population gates, and the per-day exemption
re-resolution each left all 913 tests passing. A comment explaining an invariant
is not a fence around it.

Each test below is mutation-checked -- the one-line change to `src/` that should
break it is named in its docstring, and was applied and reverted.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.employees import make_employee, place
from usali.assignments import (
    AmbiguousPrimaryError,
    MixedExemptionError,
    employee_ids_paid_by_during,
    resolve_exemption,
)
from usali.labor import promote_timecard
from usali.models import (
    Department,
    EmployeeAssignment,
    KioskDevice,
    Organization,
    PayRun,
    PayRunLine,
    PayRunLineProperty,
    Position,
    Property,
    Punch,
    Timecard,
    UsaliActualLaborFact,
    UsaliLaborFact,
)
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.onboarding import terminate_employee
from usali.overtime_rules import UnknownJurisdictionError, rules_for
from usali.reporting import _labor_variance
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_ANCHOR = date(2026, 1, 5)
_PERIOD_START = date(2026, 7, 6)
_PERIOD_END = date(2026, 7, 19)


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="58033", org_id=1, name="BW",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="OTHR", org_id=1, name="Third",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    front = Department(property_id="SJCES", name="Front Office")
    laundry = Department(property_id="58033", name="Laundry")
    db_session.add_all([front, laundry])
    db_session.flush()
    return front, laundry


# --- CRITICAL 1: the terminated employee's final paycheck --------------------


def test_an_employee_terminated_mid_period_is_still_paid_for_the_days_worked(db_session):
    """THE stranded-paycheck bug.

    `terminate_employee` sets `effective_to = last_day + 1` AND
    `status = "inactive"`. The population filtered `status == "active"`, so the
    status flip made the careful exclusive-end dating irrelevant on EVERY date.
    Someone terminated on the 10th of a period ending the 19th vanished from the
    run entirely -- approved, worked hours never submitted, no error naming them.

    Mutation: restore `EmployeeAssignment.status == "active"` to the
    `employee_ids_paid_by_during` query -> this test fails.
    """
    front, _laundry = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Leaver", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    db_session.commit()

    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp.employee_id, on_date=date(2026, 7, 10),
                       actor_subject="gm")
    db_session.commit()

    paid = employee_ids_paid_by_during(db_session, "SJCES", _PERIOD_START, _PERIOD_END)
    assert emp.employee_id in paid, (
        "someone terminated inside the period is owed wages for the days they "
        "worked; dropping them from the population loses a real paycheck silently"
    )


def test_termination_before_the_period_does_not_resurrect_the_employee(db_session):
    """The complement, so the fix is not just 'always include everyone'. Someone
    who left BEFORE the period starts must stay out.

    The termination date is chosen so `effective_to == period_start` EXACTLY --
    the boundary. An earlier version terminated five weeks early, where every
    candidate mutation excludes identically.

    Mutation: make `in_effect_on` ignore `effective_to` -> this test fails.

    NOT `overlaps_period`'s `<=` -> `<`: that mutation changes nothing
    observable, and a previous docstring here claimed it did. `overlaps_period`
    is only a PRE-FILTER now; `_owner_on_last_covered_day` walks the days and is
    authoritative, so it excludes this employee even if the pre-filter lets them
    through. That is defence in depth working, but it means the pre-filter
    boundary is not independently observable and it would be dishonest to claim
    a test covers it.
    """
    front, _laundry = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Long gone", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    db_session.commit()

    # effective_to becomes 2026-07-06 == _PERIOD_START. The boundary itself.
    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp.employee_id,
                       on_date=date(2026, 7, 5), actor_subject="gm")
    db_session.commit()

    assert emp.employee_id not in employee_ids_paid_by_during(
        db_session, "SJCES", _PERIOD_START, _PERIOD_END
    )


def test_a_mid_period_transfer_is_paid_by_the_new_property_only_once(db_session):
    """The reason the fix is not a bare overlap predicate.

    A transfer mid-period leaves TWO primary assignments overlapping it. Simply
    including anyone who overlaps would put this employee in BOTH properties'
    populations -- fixing the dropped paycheck by reintroducing DOUBLE PAYMENT.
    The rule is "the property holding the primary on the last day it was in
    force", which is single-valued in both directions.

    Mutation: drop the `newest`/`owners` selection and add every overlapping
    assignment's property -> this test fails on the `not in` assertion.
    """
    front, laundry = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Mover", pay_type="hourly")
    db_session.flush()
    transfer_on = date(2026, 7, 13)
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR, effective_to=transfer_on)
    place(db_session, emp, property_id="58033", department_id=laundry.department_id,
          is_primary=True, effective_from=transfer_on)
    db_session.commit()

    sjces = employee_ids_paid_by_during(db_session, "SJCES", _PERIOD_START, _PERIOD_END)
    bw = employee_ids_paid_by_during(db_session, "58033", _PERIOD_START, _PERIOD_END)

    assert emp.employee_id in bw, "the property they transferred TO owes the paycheck"
    assert emp.employee_id not in sjces, "one timecard, one pay run -- never both"
    assert sjces & bw == set()


# --- CRITICAL 2: exemption resolved over the period, or refused --------------


def _exempt_then_hourly(db_session, front):
    """An employee who is exempt for the first half of the period and hourly
    after -- a real mid-period demotion."""
    exempt_pos = Position(department_id=front.department_id, title="GM",
                          flsa_exempt=True)
    hourly_pos = Position(department_id=front.department_id, title="FD",
                          flsa_exempt=False)
    db_session.add_all([exempt_pos, hourly_pos])
    db_session.flush()
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Demoted", pay_type="hourly")
    db_session.flush()
    switch = date(2026, 7, 13)
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          position_id=exempt_pos.position_id, is_primary=True,
          effective_from=_ANCHOR, effective_to=switch)
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          position_id=hourly_pos.position_id, is_primary=True,
          effective_from=switch)
    return emp, switch


def test_a_mid_period_exemption_change_is_refused_not_guessed(db_session):
    """Sampling exemption once at `period_start` priced every later day under
    day 1's status -- reproduced as $160 of unpaid overtime SUBMITTED to the
    provider.

    The answer is not a better sample. How overtime accumulates across a
    mid-workweek status change is a genuine legal question (exemption turns on
    duties and salary basis assessed over the WORKWEEK), so this refuses --
    same stance as `rules_for` on an unencoded jurisdiction.

    Mutation: return `next(iter(statuses))` instead of raising -> this fails.
    """
    front, _laundry = _seed(db_session)
    emp, switch = _exempt_then_hourly(db_session, front)
    db_session.commit()

    spanning = [switch - timedelta(days=1), switch, switch + timedelta(days=1)]
    with pytest.raises(MixedExemptionError):
        resolve_exemption(db_session, emp.employee_id, spanning)


def test_a_period_wholly_inside_one_status_still_resolves(db_session):
    """The refusal must not become "refuse whenever the assignments changed".
    Days entirely on one side of the switch have an unambiguous answer.

    Mutation: raise unconditionally -> this fails.
    """
    front, _laundry = _seed(db_session)
    emp, switch = _exempt_then_hourly(db_session, front)
    db_session.commit()

    before = [switch - timedelta(days=2), switch - timedelta(days=1)]
    after = [switch, switch + timedelta(days=1)]
    assert resolve_exemption(db_session, emp.employee_id, before) is True
    assert resolve_exemption(db_session, emp.employee_id, after) is False


def test_promoting_a_card_spanning_the_change_refuses_rather_than_zero_costing(
    db_session
):
    """The estimate path, end to end. Its old shape sampled `period_start` to
    decide whether to read the rate AT ALL, so the per-day "correction" below it
    operated on a rate already set to None -- it could only turn pay off, never
    on. A card spanning a demotion promoted 80 hours at est_cost 0.

    Mutation: sample `is_exempt_on(..., card.period_start)` in `promote_timecard`
    -> this raises nothing and the card silently promotes at zero cost.
    """
    front, _laundry = _seed(db_session)
    emp, switch = _exempt_then_hourly(db_session, front)
    device = KioskDevice(property_id="SJCES", name="a", token_hash="a" * 64,
                         enrolled_by="adm")
    db_session.add(device)
    db_session.flush()
    card = Timecard(employee_id=emp.employee_id, period_start=_PERIOD_START,
                    period_end=_PERIOD_END, status="approved")
    db_session.add(card)
    db_session.flush()
    # One day either side of the demotion.
    for day, hour_in, hour_out in (
        (switch - timedelta(days=1), 9, 17),
        (switch + timedelta(days=1), 9, 17),
    ):
        for ptype, hour in (("clock_in", hour_in), ("clock_out", hour_out)):
            db_session.add(Punch(
                employee_id=emp.employee_id, kiosk_device_id=device.device_id,
                punch_type=ptype,
                punched_at=__import__("datetime").datetime(
                    day.year, day.month, day.day, hour,
                    tzinfo=__import__("datetime").timezone.utc),
                business_date=day, timecard_id=card.timecard_id,
            ))
    db_session.commit()

    with pytest.raises(MixedExemptionError):
        promote_timecard(db_session, card, anchor=_ANCHOR)


# --- CRITICAL 3: the actual-side suppression population ----------------------


def test_a_solo_departments_pay_is_not_disclosed_by_a_cross_property_headcount(
    db_session
):
    """THE disclosure.

    Vikram's paycheck is issued by SJCES (his primary) but he worked the period
    entirely at 58033, so SJCES's Front Office fact holds ALICE'S MONEY ALONE.
    The gate counted `pay_run_line`, which has no property dimension and whose
    gross is the whole cross-property paycheck -- so it saw two employees at
    SJCES and disclosed Alice's exact gross and burden. `gross / hours`
    re-derives her hourly rate.

    Mutation: drop `PayRunLineProperty.property_id == property_id` from the gate
    -> Vikram is counted at SJCES, the department reads as 2, and this fails.

    THE DEPARTMENT IS NULL ON BOTH SIDES ON PURPOSE, and an earlier version of
    this test was wrong for exactly this reason. Department ids are
    property-scoped, so two properties can never share a real one -- which means
    with distinct departments the property filter is redundant and dropping it
    changes nothing. The one key both properties DO share is NULL, the
    "Unassigned" bucket, which `department_at` returns whenever hours land at a
    property the person holds no assignment at. That is reachable (attribution is
    kiosk-derived and deliberately wider than assignment) and it is where the
    filter actually bites. The first draft asserted the right conclusion for the
    wrong reason and survived the mutation.
    """
    front, laundry = _seed(db_session)
    alice = make_employee(db_session, place_primary=False, property_id="SJCES",
                          full_name="Alice A", pay_type="hourly")
    vikram = make_employee(db_session, place_primary=False, property_id="SJCES",
                           full_name="Vikram V", pay_type="hourly")
    db_session.flush()
    place(db_session, alice, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    place(db_session, vikram, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    place(db_session, vikram, property_id="58033", department_id=laundry.department_id,
          is_primary=False, effective_from=_ANCHOR)

    run = PayRun(property_id="SJCES", period_start=_PERIOD_START,
                 period_end=_PERIOD_END, check_date=_PERIOD_END,
                 status="processed", provider="mem", created_by="pa")
    db_session.add(run)
    db_session.flush()

    # Both are paid BY SJCES...
    for emp, gross in ((alice, "160.00"), (vikram, "992.00")):
        db_session.add(PayRunLine(
            pay_run_id=run.pay_run_id, employee_id=emp.employee_id,
            hours=Decimal("8.00"), gross=gross, employee_taxes="0.00",
            employer_taxes="0.00", net=gross, department_id=front.department_id,
        ))
    # ...but Vikram's MONEY all landed at 58033. The census records that.
    db_session.add(PayRunLineProperty(
        pay_run_id=run.pay_run_id, employee_id=alice.employee_id,
        property_id="SJCES", department_id=None,
        hours=Decimal("8.00"), gross=Decimal("160.00"),
        employer_burden=Decimal("16.00"),
    ))
    db_session.add(PayRunLineProperty(
        pay_run_id=run.pay_run_id, employee_id=vikram.employee_id,
        property_id="58033", department_id=None,
        hours=Decimal("40.00"), gross=Decimal("992.00"),
        employer_burden=Decimal("99.20"),
    ))
    # SJCES's Unassigned bucket therefore contains only Alice.
    db_session.add(UsaliActualLaborFact(
        property_id="SJCES", period_start=_PERIOD_START, period_end=_PERIOD_END,
        department_id=None, hours=Decimal("8.00"),
        gross=Decimal("160.00"), employer_burden=Decimal("16.00"),
        pay_run_id=run.pay_run_id,
    ))
    db_session.commit()

    v = _labor_variance(db_session, "SJCES", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Unassigned")
    assert line.actual_gross is None, (
        "one person's money in this property's number -- disclosing it publishes "
        "Alice's exact paycheck"
    )
    assert line.employer_burden is None, "burden is ~a fixed % of gross"
    assert v.actual_total == Decimal("0.00"), "and it must reach no total either"


def test_a_zero_share_does_not_lift_a_department_over_the_floor(db_session):
    """The PRICED population, on the census. Someone whose share of THIS property
    is zero contributes no money to the disclosed figure and must not un-suppress
    it -- the D1 class, which has now recurred six times.

    Mutation: change the gate's `census.gross > 0` to `if True:` -> this fails.
    """
    front, _laundry = _seed(db_session)
    paid = make_employee(db_session, place_primary=False, property_id="SJCES",
                         full_name="Paid P", pay_type="hourly")
    unpaid = make_employee(db_session, place_primary=False, property_id="SJCES",
                           full_name="Unpaid U", pay_type="hourly")
    db_session.flush()
    for emp in (paid, unpaid):
        place(db_session, emp, property_id="SJCES",
              department_id=front.department_id, is_primary=True,
              effective_from=_ANCHOR)
    run = PayRun(property_id="SJCES", period_start=_PERIOD_START,
                 period_end=_PERIOD_END, check_date=_PERIOD_END,
                 status="processed", provider="mem", created_by="pa")
    db_session.add(run)
    db_session.flush()
    for emp, gross in ((paid, "160.00"), (unpaid, "0.00")):
        db_session.add(PayRunLineProperty(
            pay_run_id=run.pay_run_id, employee_id=emp.employee_id,
            property_id="SJCES", department_id=front.department_id,
            hours=Decimal("8.00"), gross=Decimal(gross),
            employer_burden=Decimal("0"),
        ))
    db_session.add(UsaliActualLaborFact(
        property_id="SJCES", period_start=_PERIOD_START, period_end=_PERIOD_END,
        department_id=front.department_id, hours=Decimal("8.00"),
        gross=Decimal("160.00"), employer_burden=Decimal("16.00"),
        pay_run_id=run.pay_run_id,
    ))
    db_session.commit()

    v = _labor_variance(db_session, "SJCES", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Front Office")
    assert line.actual_gross is None, (
        "only ONE employee has money here; a zero-gross row is not a second person"
    )


def test_the_estimate_side_priced_gate_also_holds(db_session):
    """The same gate on the estimate side, which the mutation pass also found
    untested. An exempt employee promotes hours at `est_cost = 0` and must not
    lift a solo department over the floor.

    Mutation: drop `and Decimal(str(lf.est_cost)) > 0` from the estimate gate
    -> this fails.
    """
    front, _laundry = _seed(db_session)
    priced = make_employee(db_session, place_primary=False, property_id="SJCES",
                           full_name="Priced P", pay_type="hourly")
    exempt = make_employee(db_session, place_primary=False, property_id="SJCES",
                           full_name="Exempt E", pay_type="salary")
    db_session.flush()
    for emp in (priced, exempt):
        place(db_session, emp, property_id="SJCES",
              department_id=front.department_id, is_primary=True,
              effective_from=_ANCHOR)
    run = PayRun(property_id="SJCES", period_start=_PERIOD_START,
                 period_end=_PERIOD_END, check_date=_PERIOD_END,
                 status="processed", provider="mem", created_by="pa")
    db_session.add(run)
    db_session.flush()
    for emp, cost in ((priced, "320.0000"), (exempt, "0.0000")):
        card = Timecard(employee_id=emp.employee_id, period_start=_PERIOD_START,
                        period_end=_PERIOD_END, status="approved")
        db_session.add(card)
        db_session.flush()
        db_session.add(UsaliLaborFact(
            property_id="SJCES", business_date=date(2026, 7, 7),
            department_id=front.department_id, hours=Decimal("16.00"),
            ot_hours=Decimal("0"), est_cost=Decimal(cost),
            timecard_id=card.timecard_id,
        ))
    db_session.commit()

    v = _labor_variance(db_session, "SJCES", date(2026, 7, 1), date(2026, 7, 31),
                        alert_pct=10)
    assert v is not None
    line = next(ln for ln in v.lines if ln.department == "Front Office")
    assert line.est_cost is None, (
        "one PRICED employee; the exempt manager's zero-cost hours are not a "
        "second person to hide behind"
    )


# --- HIGH: jurisdiction is stated, never inferred ----------------------------


def test_a_property_created_without_a_jurisdiction_refuses_to_cost(db_session):
    """`e1f0nodefault` dropped the SERVER default so a property could not
    silently inherit California overtime -- and a Python-side `default="US-CA"`
    on the model defeated it entirely, since the ORM is the only path that
    creates properties. Both guarding tests asserted on
    `information_schema.column_default` and passed while a new Texas property
    still came out Californian.

    This asserts the BEHAVIOUR instead: construct one and see what happens.

    Mutation: restore `default="US-CA"` on the model column -> this fails.
    """
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="TXAU", org_id=1, name="Austin",
                            pms_source="OPERA"))
    db_session.flush()
    prop = db_session.get(Property, "TXAU")
    assert prop is not None
    assert prop.wage_jurisdiction is None, (
        "silence must stay silence; inheriting California daily overtime and "
        "double time would miscost every hour in another state"
    )
    with pytest.raises(UnknownJurisdictionError):
        rules_for(prop.wage_jurisdiction)


# --- HIGH: cost is filed under the WORKING property's department -------------


def test_each_propertys_cost_is_filed_under_its_own_department(db_session):
    """Departments are property-scoped (unique property_id + name), so reusing
    the primary assignment's department id for every property filed 58033's
    money under a department belonging to SJCES.

    Mutation: revert `department_at(...)` in `fetch_pay_run_results` to
    `stored.department_id` -> both rows share SJCES's department and this fails.
    """
    front, laundry = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Shared S", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    place(db_session, emp, property_id="58033", department_id=laundry.department_id,
          is_primary=False, effective_from=_ANCHOR)
    db_session.commit()

    from usali.labor import department_at

    assert department_at(db_session, emp.employee_id, "SJCES", _PERIOD_END) == (
        front.department_id
    )
    assert department_at(db_session, emp.employee_id, "58033", _PERIOD_END) == (
        laundry.department_id
    ), "the other hotel's hours belong to the other hotel's department"


def test_the_owning_property_is_the_one_in_force_on_the_last_covered_day(db_session):
    """`max(effective_from)` is NOT that rule, and the docstring above it claimed
    it was while the code did something else.

    An open primary at SJCES since January plus a SHORT 58033 stint from the 8th
    to the 11th. On the 19th -- the last covered day -- the employee is
    unambiguously SJCES. Latest-start picks the 8th, so 58033 paid: wrong
    jurisdiction's overtime rules, wrong hotel's P&L, and SJCES (where they work
    12 of 14 days) dropped them entirely. The stranded paycheck, reintroduced by
    the fix for the stranded paycheck.

    Mutation: replace `_owner_on_last_covered_day` with
    `max(a.effective_from ...)` selection -> this test fails.
    """
    front, laundry = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Mostly here", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    place(db_session, emp, property_id="58033", department_id=laundry.department_id,
          is_primary=True, effective_from=date(2026, 7, 8),
          effective_to=date(2026, 7, 11))
    db_session.commit()

    sjces = employee_ids_paid_by_during(db_session, "SJCES", _PERIOD_START, _PERIOD_END)
    bw = employee_ids_paid_by_during(db_session, "58033", _PERIOD_START, _PERIOD_END)

    assert emp.employee_id in sjces, (
        "the primary in force on the last covered day is SJCES's"
    )
    assert emp.employee_id not in bw, "the stint that ended on the 11th owes nothing"


def test_one_employees_ambiguity_does_not_halt_an_unrelated_propertys_payroll(
    db_session
):
    """The refusal must land on someone who can act on it.

    The candidate query had no property filter and raised BEFORE testing whether
    the ambiguous employee was even this property's to pay. So one bad row
    anywhere in the estate halted payroll everywhere -- a refusal delivered to
    people with no connection to it, which is not "the one moment a human should
    look", it is an outage.

    Mutation: resolve every employee in the estate before the owner test ->
    this test raises AmbiguousPrimaryError and fails.
    """
    front, laundry = _seed(db_session)
    clean = make_employee(db_session, place_primary=False, property_id="SJCES",
                          full_name="Clean C", pay_type="hourly")
    tangled = make_employee(db_session, place_primary=False, property_id="58033",
                            full_name="Tangled T", pay_type="hourly")
    db_session.flush()
    place(db_session, clean, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    # Two primaries in force on the same day at DIFFERENT properties, neither of
    # them SJCES. The partial unique index only covers open rows, so a pair of
    # closed ones is entirely unconstrained -- this state is reachable.
    place(db_session, tangled, property_id="58033",
          department_id=laundry.department_id, is_primary=True,
          effective_from=date(2026, 7, 8), effective_to=date(2026, 7, 12))
    place(db_session, tangled, property_id="OTHR",
          department_id=None, is_primary=True,
          effective_from=date(2026, 7, 8), effective_to=date(2026, 7, 12))
    db_session.commit()

    # SJCES's own population is unambiguous and must resolve.
    assert employee_ids_paid_by_during(
        db_session, "SJCES", _PERIOD_START, _PERIOD_END
    ) == {clean.employee_id}

    # ...while the property that genuinely cannot tell still refuses.
    with pytest.raises(AmbiguousPrimaryError):
        employee_ids_paid_by_during(db_session, "58033", _PERIOD_START, _PERIOD_END)


def test_a_duplicate_census_row_is_rejected_by_the_database(db_session):
    """A duplicate would double a headcount and could un-suppress a solo
    department on its own, so the constraint -- not merely the writing code --
    has to forbid it.

    RENAMED: this was called `test_a_re_fetch_does_not_leave_a_stale_census` and
    claimed to cover the delete-then-rewrite path. It never performed a re-fetch.
    The re-fetch path IS covered, by
    `test_reporting_variance.py::test_full_pipeline_variance_appears_in_the_sos`
    -- disabling the census write is enough to fail it.""" 
    front, _laundry = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Solo S", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    run = PayRun(property_id="SJCES", period_start=_PERIOD_START,
                 period_end=_PERIOD_END, check_date=_PERIOD_END,
                 status="processed", provider="mem", created_by="pa")
    db_session.add(run)
    db_session.flush()
    db_session.add(PayRunLineProperty(
        pay_run_id=run.pay_run_id, employee_id=emp.employee_id,
        property_id="SJCES", department_id=front.department_id,
        hours=Decimal("8.00"), gross=Decimal("160.00"),
        employer_burden=Decimal("16.00"),
    ))
    db_session.commit()

    # The unique constraint is what makes a duplicate headcount unrepresentable.
    db_session.add(PayRunLineProperty(
        pay_run_id=run.pay_run_id, employee_id=emp.employee_id,
        property_id="SJCES", department_id=front.department_id,
        hours=Decimal("8.00"), gross=Decimal("160.00"),
        employer_burden=Decimal("16.00"),
    ))
    with pytest.raises(Exception):
        db_session.flush()
    db_session.rollback()


# --- HIGH: writing hours onto a property you have no authority over ----------


def _adjustment_world(db_engine, db_session, tmp_path):
    """Hank works BOTH hotels, so `body.property_id` is a real choice rather
    than the single obvious answer."""
    from fastapi.testclient import TestClient

    from tests.authkit import make_authkit
    from usali.db import make_session_factory
    from usali.keycloak_admin import InMemoryKeycloakAdmin
    from usali.photo_store import InMemoryPhotoStore
    from usali.server import create_app

    front, laundry = _seed(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Hank H", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES", department_id=front.department_id,
          is_primary=True, effective_from=_ANCHOR)
    place(db_session, emp, property_id="58033", department_id=laundry.department_id,
          is_primary=False, effective_from=_ANCHOR)
    card = Timecard(employee_id=emp.employee_id, period_start=_PERIOD_START,
                    period_end=_PERIOD_END, status="open")
    db_session.add(card)
    db_session.commit()
    # L4: role authority is DB grants, not token roles (scope stays
    # with the claim — the SJCES-scoped GM must stay refused at 58033).
    from tests.grants import grant_role

    grant_role(db_session, "org_admin", sub="adm")
    grant_role(db_session, "property_gm", sub="gm", property_id="SJCES")

    verifier, mint = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
    )
    return TestClient(app), mint, card, emp


def _adjustment_body(property_id):
    return {
        "punch_type": "clock_in",
        "adjusted_at": "2026-07-08T06:00:00+00:00",
        "business_date": "2026-07-08",
        "reason": "missed clock in",
        "property_id": property_id,
    }


def test_a_manager_cannot_write_hours_onto_a_property_they_do_not_scope(
    db_engine, db_session, tmp_path
):
    """`_require_card_access` checks the CARD's property -- the employee's
    primary -- which says nothing about the property being WRITTEN TO. A GM
    scoped to SJCES could post synthetic hours onto 58033's Schedule 14 and its
    labor cost. Same class as the `terminate` bypass, different verb.

    Mutation: delete the `_require_property_access(...)` call on
    `body.property_id` -> this returns 201 and the test fails.
    """
    c, mint, card, _emp = _adjustment_world(db_engine, db_session, tmp_path)
    sjces_gm = mint(roles=["property_gm"], sub="gm",
                    scopes=[{"property_id": "SJCES", "department_id": None}])

    r = c.post(f"/api/timecards/{card.timecard_id}/adjustments",
               json=_adjustment_body("58033"),
               headers={"Authorization": f"Bearer {sjces_gm}"})

    assert r.status_code == 403, (
        "hours written here become labor cost on that hotel's P&L; authority "
        "over the card is not authority over the target property"
    )


def test_a_manager_can_still_correct_hours_at_a_property_they_do_scope(
    db_engine, db_session, tmp_path
):
    """The check must not become "adjustments are impossible". The same GM
    correcting hours at their OWN hotel still works.

    Mutation: make `_require_property_access` reject unconditionally -> fails.
    """
    c, mint, card, _emp = _adjustment_world(db_engine, db_session, tmp_path)
    sjces_gm = mint(roles=["property_gm"], sub="gm",
                    scopes=[{"property_id": "SJCES", "department_id": None}])

    r = c.post(f"/api/timecards/{card.timecard_id}/adjustments",
               json=_adjustment_body("SJCES"),
               headers={"Authorization": f"Bearer {sjces_gm}"})

    assert r.status_code == 201


def test_an_unplaced_employee_is_not_a_free_choice_of_property(
    db_engine, db_session, tmp_path
):
    """The guard was `if known and body.property_id not in placements`, so when
    the employee had NO placement on that date the check was skipped entirely
    and any FK-valid property was accepted -- putting a terminated employee's
    final corrections onto a P&L they never worked at. The `known` escape is
    gone.

    Mutation: restore `known and ...` -> this returns 201 and the test fails.
    """
    c, mint, card, emp = _adjustment_world(db_engine, db_session, tmp_path)
    # Placements START on the 10th, so the 8th predates them. Deliberately NOT
    # done by closing them early: an employee with no placement at period_end
    # has no card property at all and 409s before this check is ever reached.
    for a in db_session.scalars(
        select(EmployeeAssignment).where(EmployeeAssignment.employee_id == emp.employee_id)
    ):
        a.effective_from = date(2026, 7, 10)
    db_session.commit()

    admin = mint(roles=["org_admin"], sub="adm")
    r = c.post(f"/api/timecards/{card.timecard_id}/adjustments",
               json=_adjustment_body("58033"),
               headers={"Authorization": f"Bearer {admin}"})

    assert r.status_code == 422, (
        "no placement on that date is not permission to pick a P&L"
    )
