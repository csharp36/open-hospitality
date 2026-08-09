"""The one proportional split, brute-forced.

Three hand-rolled copies of this algorithm existed; adversarial review found two
defective in the same two ways. The contract is exact summation, so the tests
are exhaustive rather than illustrative.
"""

from decimal import Decimal

import pytest

from usali.apportion import InexactApportionError, apportion

_CENTS = Decimal("0.01")


def _sum(shares):
    return sum(shares.values(), Decimal("0"))


# --- the contract, brute-forced ---------------------------------------------

@pytest.mark.parametrize("total_cents", [0, 1, 2, 7, 13, 59, 100, 397, 1439, 100000])
@pytest.mark.parametrize(
    "weights",
    [
        {"a": 1},
        {"a": 1, "b": 1},
        {"a": 6, "b": 5},
        {"a": 1, "b": 2, "c": 3},
        {"a": 7, "b": 13, "c": 137, "d": 480},
        {"a": 1, "b": 0},
        {"a": 0, "b": 0, "c": 1},
    ],
)
def test_parts_always_sum_exactly(total_cents, weights):
    total = Decimal(total_cents) * _CENTS
    shares = apportion(total, weights, quantum=_CENTS)
    assert _sum(shares) == total


@pytest.mark.parametrize("total_cents", [1, 7, 100, 397])
def test_negative_totals_do_not_invent_money(total_cents):
    """The defect that broke clawbacks: ROUND_DOWN truncates toward zero, so a
    negative total's floored parts summed to MORE than the total and the
    remainder pass never ran. -100.00 split 6:5 came to -99.99."""
    total = Decimal(-total_cents) * _CENTS
    shares = apportion(total, {"a": 6, "b": 5}, quantum=_CENTS)
    assert _sum(shares) == total
    assert all(v <= 0 for v in shares.values()), "signs must match the total"


def test_the_specific_reproduction_from_review():
    shares = apportion(Decimal("-100.00"), {"a": 6, "b": 5}, quantum=_CENTS)
    assert _sum(shares) == Decimal("-100.00")


def test_sub_cent_totals_are_refused_not_silently_dropped():
    """8.125 split evenly used to give 4.06 + 4.06 = 8.12, losing 0.005 without
    a word. A provider adapter is free to return 3dp gross."""
    with pytest.raises(InexactApportionError):
        apportion(Decimal("8.125"), {"a": 1, "b": 1}, quantum=_CENTS)


def test_no_share_exceeds_the_total_or_flips_sign():
    shares = apportion(Decimal("0.01"), {"a": 1, "b": 1, "c": 1}, quantum=_CENTS)
    assert _sum(shares) == Decimal("0.01")
    assert all(Decimal("0") <= v <= Decimal("0.01") for v in shares.values())


# --- degenerate inputs that used to raise ------------------------------------

def test_all_zero_weights_returns_zeros_rather_than_dividing_by_zero():
    """Previously `decimal.InvalidOperation`. There is no defensible split, so
    give everything to nobody rather than inventing a distribution."""
    shares = apportion(Decimal("10.00"), {"a": 0, "b": 0}, quantum=_CENTS)
    assert shares == {"a": Decimal("0"), "b": Decimal("0")}


def test_empty_weights_returns_empty():
    assert apportion(Decimal("10.00"), {}, quantum=_CENTS) == {}


def test_zero_total_gives_every_key_zero():
    shares = apportion(Decimal("0"), {"a": 3, "b": 1}, quantum=_CENTS)
    assert shares == {"a": Decimal("0"), "b": Decimal("0")}


# --- determinism -------------------------------------------------------------

def test_tie_breaking_is_deterministic():
    """Equal weights with an odd remainder must land the same way every run, or
    two identical pay runs disagree by a cent."""
    first = apportion(Decimal("0.03"), {"a": 1, "b": 1, "c": 1, "d": 1}, quantum=_CENTS)
    for _ in range(20):
        assert apportion(
            Decimal("0.03"), {"d": 1, "c": 1, "b": 1, "a": 1}, quantum=_CENTS
        ) == first


def test_integer_quantum_supports_minutes():
    """timecards splits whole minutes; same helper, quantum of 1."""
    shares = apportion(Decimal(631), {"a": 360, "b": 271}, quantum=Decimal(1))
    assert _sum(shares) == Decimal(631)
    assert all(v == v.to_integral_value() for v in shares.values())
