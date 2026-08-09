"""Rate resolution: what does this placement pay on this date?

The counterpart to `usali.assignments`. Every consumer of a rate goes through
`rate_on`, for the same reason every consumer of a placement goes through
`primary_assignment_on`: an ad-hoc date comparison at a call site is how a raise
entered today silently re-costs a closed quarter, and the arithmetic succeeds so
nothing downstream signals it.
"""

from datetime import date
from decimal import Decimal

import pytest

from tests.employees import make_employee, place
from usali.models import AssignmentRate, Organization, Property
from usali.rates import (
    AmbiguousRateError,
    BelowStatutoryFloorError,
    MissingBaseRateError,
    hourly_rates_on,
    rate_on,
)
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

ANCHOR = date(2026, 1, 5)


def _seed(db_session, *, jurisdiction: str = "US-CA"):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(
        Property(
            property_id="SJCES",
            org_id=1,
            name="HIE SJC",
            pms_source="OPERA",
            wage_jurisdiction=jurisdiction,
        )
    )
    db_session.flush()
    employee = make_employee(
        db_session,
        place_primary=False,
        property_id="SJCES",
        full_name="Vikram Jindal",
        pay_type="hourly",
    )
    assignment = place(
        db_session,
        employee,
        property_id="SJCES",
        is_primary=True,
        effective_from=ANCHOR,
    )
    return employee, assignment


def _rate(
    db_session,
    assignment,
    *,
    rate_type: str = "regular",
    amount: str,
    effective_from: date = ANCHOR,
    effective_to: date | None = None,
) -> None:
    db_session.add(
        AssignmentRate(
            assignment_id=assignment.assignment_id,
            rate_type=rate_type,
            amount=amount,
            effective_from=effective_from,
            effective_to=effective_to,
        )
    )
    db_session.flush()


# --- what is in effect -------------------------------------------------------


def test_resolves_the_rate_in_effect_on_the_date(db_session):
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")

    assert rate_on(
        db_session, assignment.assignment_id, "regular", date(2026, 4, 15)
    ) == Decimal("20.00")


def test_a_raise_applies_from_its_effective_date_and_not_before(db_session):
    """THE phase invariant, at resolver scope. A rate effective 1 August does
    not apply on 31 July, so re-costing April cannot pick up an August raise."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00", effective_to=date(2026, 8, 1))
    _rate(db_session, assignment, amount="30.00", effective_from=date(2026, 8, 1))

    assert rate_on(
        db_session, assignment.assignment_id, "regular", date(2026, 7, 31)
    ) == Decimal("20.00")
    assert rate_on(
        db_session, assignment.assignment_id, "regular", date(2026, 8, 1)
    ) == Decimal("30.00")


def test_no_rate_before_the_anchor_resolves_to_none_not_zero(db_session):
    """No history was imported, so dates before the anchor have no rate. `None`
    means "cannot cost"; zero is a PRICED value and would enter the suppression
    population as real cost, un-suppressing a solo department."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")

    assert rate_on(db_session, assignment.assignment_id, "regular", date(2025, 12, 31)) is None


def test_an_absent_rate_type_resolves_to_none(db_session):
    """`ot`/`dot`/`holiday` are not backfilled. Absent means "use the
    jurisdiction rule" — today's behaviour — not "unpaid"."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")

    assert rate_on(db_session, assignment.assignment_id, "ot", date(2026, 4, 15)) is None


def test_a_closed_rate_does_not_apply_on_its_end_date(db_session):
    """Exclusive end, identical to `EmployeeAssignment`."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00", effective_to=date(2026, 4, 15))

    assert rate_on(db_session, assignment.assignment_id, "regular", date(2026, 4, 14)) == Decimal("20.00")
    assert rate_on(db_session, assignment.assignment_id, "regular", date(2026, 4, 15)) is None


def test_two_rates_in_force_on_one_date_are_refused_not_ranked(db_session):
    """Two answers to "what does this person earn" is a data error. Picking one
    prices real hours at a figure nobody chose. The partial unique index forbids
    two OPEN rates; two overlapping CLOSED ranges reach here."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00", effective_to=date(2026, 6, 1))
    _rate(
        db_session,
        assignment,
        amount="25.00",
        effective_from=date(2026, 3, 1),
        effective_to=date(2026, 9, 1),
    )

    with pytest.raises(AmbiguousRateError) as excinfo:
        rate_on(db_session, assignment.assignment_id, "regular", date(2026, 4, 15))
    assert "2026-04-15" in str(excinfo.value)


# --- the statutory floor -----------------------------------------------------


def test_a_stored_ot_rate_above_the_floor_is_honoured(db_session):
    """The union/contract case this feature exists for. $35 against a CA floor
    of $30 is lawful and is exactly what a stored premium expresses."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")
    _rate(db_session, assignment, rate_type="ot", amount="35.00")

    assert rate_on(db_session, assignment.assignment_id, "ot", date(2026, 4, 15)) == Decimal("35.00")


def test_a_stored_ot_rate_equal_to_the_floor_is_honoured(db_session):
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")
    _rate(db_session, assignment, rate_type="ot", amount="30.00")

    assert rate_on(db_session, assignment.assignment_id, "ot", date(2026, 4, 15)) == Decimal("30.00")


def test_a_stored_ot_rate_below_the_floor_is_refused(db_session):
    """Not silently lifted to the floor — that pays a figure no record shows —
    and not silently honoured, which underpays against a rule we hold a
    citation for."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")
    _rate(db_session, assignment, rate_type="ot", amount="28.00")

    with pytest.raises(BelowStatutoryFloorError) as excinfo:
        rate_on(db_session, assignment.assignment_id, "ot", date(2026, 4, 15))
    message = str(excinfo.value)
    # NAMES NO FIGURE (Task-7 disclosure fix): the stored rate, the regular
    # rate, and the derived floor are all compensation, and this message reaches
    # request logs. It stays actionable via rate type + date + jurisdiction; the
    # admin reads the value through the audited GET /pay-rate.
    assert "28.00" not in message and "20.00" not in message, "no stored/regular rate"
    assert "30.00" not in message, "no derived floor (it discloses the regular rate)"
    assert "ot" in message, "names the rate type"
    assert "2026-04-15" in message, "names the date"
    assert "US-CA" in message, "names the jurisdiction imposing it"


def test_a_stored_double_time_rate_below_twice_regular_is_refused(db_session):
    """The floor is per rate type: `dot` is checked against 2x, not 1.5x. A
    single multiplier here would pass $35 double-time against a $40 floor."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")
    _rate(db_session, assignment, rate_type="dot", amount="35.00")

    with pytest.raises(BelowStatutoryFloorError) as excinfo:
        rate_on(db_session, assignment.assignment_id, "dot", date(2026, 4, 15))
    message = str(excinfo.value)
    # The refusal still fires (dot checked against 2x, not 1.5x — a $35 dot
    # against a $40 floor is below it); it just names no figure.
    assert "dot" in message and "US-CA" in message
    assert "35.00" not in message and "40.00" not in message, "no compensation figure"


def test_the_floor_is_read_on_the_asked_date_not_the_latest_regular(db_session):
    """An OT rate lawful in April must not be refused because a later raise
    moved the floor above it — the floor is a fact about the date being costed."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00", effective_to=date(2026, 8, 1))
    _rate(db_session, assignment, amount="40.00", effective_from=date(2026, 8, 1))
    _rate(db_session, assignment, rate_type="ot", amount="30.00", effective_to=date(2026, 8, 1))

    assert rate_on(db_session, assignment.assignment_id, "ot", date(2026, 4, 15)) == Decimal("30.00")


def test_a_holiday_rate_below_regular_is_honoured(db_session):
    """Nothing in the engine has a concept of a holiday, so there is no floor to
    check. Stored and used as given rather than measured against a rule that
    does not exist."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")
    _rate(db_session, assignment, rate_type="holiday", amount="15.00")

    assert rate_on(db_session, assignment.assignment_id, "holiday", date(2026, 4, 15)) == Decimal("15.00")


def test_double_time_carries_no_floor_where_the_jurisdiction_has_none(db_session):
    """Under the FLSA baseline there is no double-time rule, so there is no
    statutory figure to fall below. Inventing California's 2x here would refuse
    lawful data in every state but one."""
    _, assignment = _seed(db_session, jurisdiction="US")
    _rate(db_session, assignment, amount="20.00")
    _rate(db_session, assignment, rate_type="dot", amount="25.00")

    assert rate_on(db_session, assignment.assignment_id, "dot", date(2026, 4, 15)) == Decimal("25.00")


def test_a_premium_rate_with_no_regular_to_measure_it_against_is_refused(db_session):
    """The floor is unverifiable, so the rate is not usable. Honouring it would
    pay a premium the system cannot show is lawful; the same fail-closed stance
    as `is_exempt_on` treating silence as non-exempt."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, rate_type="ot", amount="30.00")

    with pytest.raises(MissingBaseRateError):
        rate_on(db_session, assignment.assignment_id, "ot", date(2026, 4, 15))


def test_an_absent_premium_rate_needs_no_regular_rate(db_session):
    """The floor check is about STORED premiums. Asking for an OT rate that was
    never stored resolves to None on a placement with no rate at all, rather
    than raising about a base rate nobody asked for."""
    _, assignment = _seed(db_session)

    assert rate_on(db_session, assignment.assignment_id, "ot", date(2026, 4, 15)) is None


# --- the costing view: all three hourly rates, floors and fallbacks applied ---


def test_hourly_rates_fall_back_to_the_jurisdiction_multipliers(db_session):
    """Nothing backfilled `ot`/`dot`, and absent means "use the jurisdiction
    rule". Falling back HERE rather than in each consumer is the point: the
    multiplier had two module-level copies before this, free to drift."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")

    rates = hourly_rates_on(db_session, assignment.assignment_id, date(2026, 4, 15))
    assert rates is not None
    assert rates.regular == Decimal("20.00")
    assert rates.ot == Decimal("30.00"), "1.5x under CA"
    assert rates.dot == Decimal("40.00"), "2x under CA"


def test_a_stored_premium_overrides_the_derived_one(db_session):
    """The whole reason all four types are stored: a union or contract premium
    above the floor must actually be paid, not recomputed away."""
    _, assignment = _seed(db_session)
    _rate(db_session, assignment, amount="20.00")
    _rate(db_session, assignment, rate_type="ot", amount="35.00")

    rates = hourly_rates_on(db_session, assignment.assignment_id, date(2026, 4, 15))
    assert rates is not None
    assert rates.ot == Decimal("35.00")
    assert rates.dot == Decimal("40.00"), "dot still derived; only ot was stored"


def test_no_regular_rate_means_no_rates_at_all(db_session):
    """`None`, not a zeroed rate set. Callers promote hours with no cost, which
    keeps Schedule 15 complete while keeping the employee OUT of the priced
    population every suppression gate counts."""
    _, assignment = _seed(db_session)

    assert hourly_rates_on(db_session, assignment.assignment_id, date(2026, 4, 15)) is None


def test_double_time_is_none_where_the_jurisdiction_has_no_such_rule(db_session):
    """Under the FLSA baseline the engine can never emit double-time hours, so
    there is no rate to invent. `None` beats a fabricated 2x that would quietly
    become real money if a jurisdiction were ever mis-encoded."""
    _, assignment = _seed(db_session, jurisdiction="US")
    _rate(db_session, assignment, amount="20.00")

    rates = hourly_rates_on(db_session, assignment.assignment_id, date(2026, 4, 15))
    assert rates is not None
    assert rates.ot == Decimal("30.00")
    assert rates.dot is None
