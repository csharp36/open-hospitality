"""E5 Task 2: the ONE definition of a legal allocation chain.

`allocation_violation` is consumed by two doors that must never drift — the
vault API (refusing a bad chain at entry) and pay-run preflight (refusing to
send one that got in anyway). It sees ONLY allocation metadata: no account or
routing value is ever passed in, so no refusal message can leak one.
"""

from decimal import Decimal

from usali.deposit_accounts import AllocationSpec, allocation_violation


def _spec(kind, value=None):
    return AllocationSpec(
        allocation_type=kind,
        allocation_value=None if value is None else Decimal(value),
    )


def test_an_empty_chain_is_legal_here():
    """No accounts is not an allocation problem — whether deposit data is
    REQUIRED is preflight's per-employee completeness check, a different
    question with a different owner."""
    assert allocation_violation([]) is None


def test_a_single_remainder_is_the_ordinary_case():
    assert allocation_violation([_spec("remainder")]) is None


def test_amounts_and_percents_then_a_remainder_are_legal():
    assert allocation_violation([
        _spec("amount", "50.00"),
        _spec("percent", "25.00"),
        _spec("remainder"),
    ]) is None


def test_a_chain_without_a_remainder_is_refused():
    """percent 100 exactly is NOT an acceptable substitute: rounding on the
    provider side can strand cents, and 'exactly 100' silently becomes
    'exactly 99' the first time someone edits one row. The remainder account
    is the structural answer to 'where does the rest go'."""
    msg = allocation_violation([_spec("percent", "100.00")])
    assert msg is not None and "remainder" in msg
    msg = allocation_violation([_spec("amount", "50.00")])
    assert msg is not None and "remainder" in msg


def test_two_remainders_are_refused():
    msg = allocation_violation([_spec("remainder"), _spec("remainder")])
    assert msg is not None and "one" in msg


def test_a_remainder_anywhere_but_last_is_refused():
    msg = allocation_violation([_spec("remainder"), _spec("amount", "50.00")])
    assert msg is not None and "last" in msg


def test_percents_summing_to_100_or_more_are_refused():
    """100 would starve the remainder by construction — refused, not
    normalized: silently rewriting someone's allocation is money arithmetic
    nobody asked for. 99 is the legal boundary."""
    assert allocation_violation([
        _spec("percent", "99.00"), _spec("remainder"),
    ]) is None
    msg = allocation_violation([
        _spec("percent", "60.00"), _spec("percent", "40.00"), _spec("remainder"),
    ])
    assert msg is not None and "100" in msg


def test_a_zero_or_negative_amount_is_refused():
    for bad in ("0.00", "-25.00"):
        msg = allocation_violation([_spec("amount", bad), _spec("remainder")])
        assert msg is not None


def test_a_percent_outside_the_open_interval_is_refused():
    for bad in ("0.00", "-1.00", "100.00"):
        msg = allocation_violation([_spec("percent", bad), _spec("remainder")])
        assert msg is not None
    # Fractional percents inside the interval are fine — the SUM rule guards
    # the total, not the row.
    assert allocation_violation(
        [_spec("percent", "99.50"), _spec("remainder")]
    ) is None


def test_an_unknown_allocation_type_is_refused():
    msg = allocation_violation([_spec("balance"), _spec("remainder")])
    assert msg is not None


def test_a_value_on_a_remainder_and_a_valueless_amount_are_refused():
    """Mirrors the CHECK constraint — the function and the schema must agree,
    and the function speaks first with a 422 instead of a 500."""
    assert allocation_violation([_spec("remainder", "10.00")]) is not None
    assert allocation_violation(
        [_spec("amount"), _spec("remainder")]
    ) is not None


def test_messages_never_carry_the_values_they_judge():
    """The refusal names the rule, not the figure: allocation values are not
    secret, but these messages travel to logs and API clients beside PII-
    adjacent context, and the C2 lesson is that interpolated field echoes
    leak. Rule references like '100' are fine; the caller's own numbers are
    not."""
    msg = allocation_violation([
        _spec("amount", "1234.56"), _spec("percent", "77.00"),
    ])
    assert msg is not None
    assert "1234.56" not in msg and "77" not in msg
