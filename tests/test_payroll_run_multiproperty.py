"""Pay runs across two properties.

THE headline invariant: one timecard reaches exactly one pay run. Before
assignments, the population predicate was `Employee.property_id == property_id`;
with an employee assigned to two properties that matched the same timecard TWICE
and paid them twice. Not a reporting error -- a double payment.

The counterpart invariant is that decoupling gross from cost does not lose
money: the employee is paid once, but BOTH hotels' P&Ls still carry their true
share of the labor.
"""

from datetime import date, timedelta


from tests.employees import make_employee, set_rate_everywhere
from usali.assignments import employee_ids_with_primary_at
from usali.models import (
    Department,
    EmployeeAssignment,
    Organization,
    Property,
)

_ANCHOR = date(2026, 1, 5)
_PERIOD_END = _ANCHOR + timedelta(days=13)


def _seed_two_properties(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="58033", org_id=1, name="BW", pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    front = Department(property_id="SJCES", name="Front Office")
    laundry = Department(property_id="58033", name="Laundry")
    db_session.add_all([front, laundry])
    db_session.flush()
    return front, laundry


def _employee(db_session, name, *, primary, also=None, front=None, laundry=None):
    emp = make_employee(db_session, place_primary=False, property_id=primary, full_name=name, pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id=primary,
        department_id=(front.department_id if primary == "SJCES" else laundry.department_id),
        is_primary=True, status="active", effective_from=_ANCHOR,
    ))
    if also is not None:
        db_session.add(EmployeeAssignment(
            employee_id=emp.employee_id, property_id=also,
            department_id=(front.department_id if also == "SJCES" else laundry.department_id),
            is_primary=False, status="active", effective_from=_ANCHOR,
        ))
    db_session.flush()
    # A rate on EVERY placement. The pay run refuses an employee whose worked
    # days resolve to more than one rate, so a seed that rated only the primary
    # would be testing that refusal by accident rather than the split.
    set_rate_everywhere(db_session, emp, "20.00")
    return emp


def test_a_dual_property_employee_is_paid_by_exactly_one_property(db_session):
    """THE test this task exists for. Vikram works both hotels; only the
    property holding his PRIMARY assignment may pay him."""
    front, laundry = _seed_two_properties(db_session)
    vikram = _employee(db_session, "Vikram Jindal", primary="SJCES", also="58033",
                       front=front, laundry=laundry)
    db_session.commit()

    sjces = employee_ids_with_primary_at(db_session, "SJCES", _PERIOD_END)
    bw = employee_ids_with_primary_at(db_session, "58033", _PERIOD_END)

    assert vikram.employee_id in sjces
    assert vikram.employee_id not in bw, "employee would be paid by both pay runs"
    assert sjces & bw == set()


def test_the_two_pay_run_populations_never_overlap(db_session):
    """Generalised: no employee may appear in both properties' populations, no
    matter how their assignments are arranged."""
    front, laundry = _seed_two_properties(db_session)
    _employee(db_session, "SJCES only", primary="SJCES", front=front, laundry=laundry)
    _employee(db_session, "58033 only", primary="58033", front=front, laundry=laundry)
    _employee(db_session, "Both, pays SJCES", primary="SJCES", also="58033",
              front=front, laundry=laundry)
    _employee(db_session, "Both, pays 58033", primary="58033", also="SJCES",
              front=front, laundry=laundry)
    db_session.commit()

    sjces = employee_ids_with_primary_at(db_session, "SJCES", _PERIOD_END)
    bw = employee_ids_with_primary_at(db_session, "58033", _PERIOD_END)

    assert sjces & bw == set()
    assert len(sjces) == 2 and len(bw) == 2
    assert len(sjces | bw) == 4, "every employee is paid by exactly one property"


def test_a_secondary_assignment_never_confers_payment(db_session):
    """Working somewhere is not the same as being paid by it. This is precisely
    the distinction the old `Employee.property_id` predicate could not make."""
    front, laundry = _seed_two_properties(db_session)
    emp = _employee(db_session, "Vikram Jindal", primary="SJCES", also="58033",
                    front=front, laundry=laundry)
    db_session.commit()

    from usali.assignments import property_ids_on

    # Assigned to (and may be costed at) both...
    assert property_ids_on(db_session, emp.employee_id, _PERIOD_END) == {"SJCES", "58033"}
    # ...but paid by exactly one.
    assert emp.employee_id not in employee_ids_with_primary_at(db_session, "58033", _PERIOD_END)


def test_an_ended_primary_assignment_stops_conferring_payment(db_session):
    """Someone who transferred mid-year must not keep drawing a paycheck from
    the property they left."""
    front, laundry = _seed_two_properties(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES", full_name="Mover", pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    db_session.add_all([
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="SJCES",
            department_id=front.department_id, is_primary=True, status="active",
            effective_from=_ANCHOR, effective_to=date(2026, 1, 12),
        ),
        EmployeeAssignment(
            employee_id=emp.employee_id, property_id="58033",
            department_id=laundry.department_id, is_primary=True, status="active",
            effective_from=date(2026, 1, 12),
        ),
    ])
    db_session.commit()

    # Before the transfer date, SJCES pays.
    assert emp.employee_id in employee_ids_with_primary_at(db_session, "SJCES", date(2026, 1, 11))
    # On and after it, 58033 does -- and SJCES no longer.
    assert emp.employee_id in employee_ids_with_primary_at(db_session, "58033", date(2026, 1, 12))
    assert emp.employee_id not in employee_ids_with_primary_at(
        db_session, "SJCES", date(2026, 1, 12)
    )


def test_an_inactive_assignment_confers_no_payment(db_session):
    """A terminated employee must drop out of the pay run."""
    front, laundry = _seed_two_properties(db_session)
    emp = make_employee(db_session, place_primary=False, property_id="SJCES", full_name="Gone", pay_type="hourly")
    db_session.add(emp)
    db_session.flush()
    db_session.add(EmployeeAssignment(
        employee_id=emp.employee_id, property_id="SJCES",
        department_id=front.department_id, is_primary=True, status="inactive",
        effective_from=_ANCHOR,
    ))
    db_session.commit()

    assert emp.employee_id not in employee_ids_with_primary_at(
        db_session, "SJCES", _PERIOD_END
    )


def test_an_unassigned_employee_is_paid_by_nobody(db_session):
    """Task 9 deleted the ramp that let `Employee.property_id` put someone in a
    pay run. An employee row with no assignment is now unpayable ANYWHERE rather
    than payable by default at whatever property the column happened to name.

    Failing closed is the right direction for a population that writes checks:
    a missing assignment surfaces as someone absent from a run, which a manager
    notices, rather than as a payment nobody authorized.
    """
    front, laundry = _seed_two_properties(db_session)

    # An employee whose paycheck comes from SJCES while also working 58033 --
    # the shape that would double-pay if the population matched on "works here".
    assigned = make_employee(db_session, place_primary=False, property_id="58033", full_name="Assigned", pay_type="hourly")
    db_session.add(assigned)
    db_session.flush()
    db_session.add_all([
        EmployeeAssignment(
            employee_id=assigned.employee_id, property_id="SJCES",
            department_id=front.department_id, is_primary=True, status="active",
            effective_from=_ANCHOR,
        ),
        EmployeeAssignment(
            employee_id=assigned.employee_id, property_id="58033",
            department_id=laundry.department_id, is_primary=False, status="active",
            effective_from=_ANCHOR,
        ),
    ])

    # No assignments at all.
    unassigned = make_employee(db_session, place_primary=False, property_id="58033",
                               full_name="Unassigned", pay_type="hourly")
    db_session.add(unassigned)
    db_session.commit()

    bw = employee_ids_with_primary_at(db_session, "58033", _PERIOD_END)
    sjces = employee_ids_with_primary_at(db_session, "SJCES", _PERIOD_END)
    assert unassigned.employee_id not in bw and unassigned.employee_id not in sjces, (
        "an employee with no assignment must be payable by NO property -- the "
        "column that used to volunteer one is gone"
    )
    # And the paycheck still comes from exactly one property, not both.
    assert assigned.employee_id in sjces
    assert assigned.employee_id not in bw


# --- the end-to-end write this file originally failed to exercise ------------
#
# The population tests above check WHO is paid. They never called
# fetch_pay_run_results, so the per-property cost allocation -- the actual
# headline of E1 Task 6 -- shipped with zero coverage and a unique-constraint
# violation that made it impossible to execute. An adversarial review found it.

from datetime import UTC, datetime  # noqa: E402
from decimal import Decimal  # noqa: E402

from sqlalchemy import select  # noqa: E402

from usali.models import (  # noqa: E402
    EmployeePayrollProfile,
    KioskDevice,
    PaySchedule,
    Position,
    Punch,
    UsaliActualLaborFact,
)
from usali.opener import SoftwareOpener, seal_for_test  # noqa: E402
from usali.payroll_provider import InMemoryPayrollProvider  # noqa: E402
from usali.payroll_run import execute_pay_run, fetch_pay_run_results  # noqa: E402
from usali.timecards import assemble_timecard  # noqa: E402
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS  # noqa: E402

_OPENER = SoftwareOpener.generate(key_id="test-e1-multiprop")
_PERIOD_DAY = date(2026, 1, 6)


def _sealed(db_session, emp_id):
    def s(field, plain):
        return seal_for_test(
            _OPENER.public_key(), plain, aad=f"{emp_id}:{field}".encode()
        ).to_json()

    from usali.deposit_accounts import account_slot, routing_slot
    from usali.models import DepositAccount

    db_session.add(EmployeePayrollProfile(
        employee_id=emp_id,
        ssn_sealed=s("ssn", b"123-45-6789"),
    ))
    db_session.add(DepositAccount(
        employee_id=emp_id, ordinal=1, allocation_type="remainder",
        allocation_value=None, account_type="checking",
        sealed_account=s(account_slot(1, False), b"12345678"),
        sealed_routing=s(routing_slot(1, False), b"021000021"),
        legacy_sealed=False,
    ))


def test_cost_splits_across_properties_and_sums_to_the_gross_paid(db_session):
    """The write that could not execute: a shared employee produced two rows
    with the same (pay_run_id, department_id) and raised UniqueViolation.

    One paycheck, two P&Ls -- and the parts must sum to the whole.
    """
    front, laundry = _seed_two_properties(db_session)
    db_session.add(PaySchedule(property_id="SJCES", frequency="biweekly",
                               anchor=_ANCHOR, check_date_offset_days=5))
    pos = Position(department_id=front.department_id, title="FD", flsa_exempt=False)
    db_session.add(pos)
    sjces_kiosk = KioskDevice(property_id="SJCES", name="a", token_hash="q" * 64,
                              enrolled_by="admin")
    bw_kiosk = KioskDevice(property_id="58033", name="b", token_hash="r" * 64,
                           enrolled_by="admin")
    db_session.add_all([sjces_kiosk, bw_kiosk])
    db_session.flush()

    emp = _employee(db_session, "Vikram Jindal", primary="SJCES", also="58033",
                    front=front, laundry=laundry)
    _sealed(db_session, emp.employee_id)
    for device, hours in ((sjces_kiosk, (9, 15)), (bw_kiosk, (16, 20))):
        for ptype, h in (("clock_in", hours[0]), ("clock_out", hours[1])):
            db_session.add(Punch(
                employee_id=emp.employee_id, kiosk_device_id=device.device_id,
                punch_type=ptype,
                punched_at=datetime(2026, 1, 6, h, tzinfo=UTC),
                business_date=_PERIOD_DAY,
                photo_key=f"k/{emp.employee_id}/{device.device_id}/{ptype}",
            ))
    db_session.commit()

    card = assemble_timecard(db_session, emp.employee_id, _PERIOD_DAY, anchor=_ANCHOR)
    card.status = "approved"
    card.approved_by = "gm"
    card.approved_at = datetime.now(UTC)
    db_session.commit()

    provider = InMemoryPayrollProvider()
    run = execute_pay_run(db_session, "SJCES", _PERIOD_DAY, anchor=_ANCHOR,
                          provider=provider, provider_name="mem",
                          opener=_OPENER, actor="pa")
    db_session.commit()

    # This raised UniqueViolation before the key was widened to include property.
    fetch_pay_run_results(db_session, run, provider=provider)
    db_session.commit()

    facts = db_session.execute(select(UsaliActualLaborFact)).scalars().all()
    by_property = {f.property_id: f for f in facts}

    assert set(by_property) == {"SJCES", "58033"}, (
        "cost must reach BOTH properties -- one paycheck, two P&Ls"
    )
    # 6h at SJCES + 4h at 58033: the split follows hours worked.
    assert by_property["SJCES"].hours == Decimal("6.00")
    assert by_property["58033"].hours == Decimal("4.00")
    # And the parts sum exactly to what was actually paid.
    from usali.models import PayRunLine

    line_gross = sum(
        Decimal(str(line.gross))
        for line in db_session.execute(
            select(PayRunLine).where(PayRunLine.pay_run_id == run.pay_run_id)
        ).scalars()
    )
    assert sum(Decimal(str(f.gross)) for f in facts) == line_gross
