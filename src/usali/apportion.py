"""Exact proportional splitting — ONE implementation, all callers.

There were three hand-rolled largest-remainder splits (`timecards._apportion`,
`labor._apportion_hours`, `payroll_run._weighted`). Adversarial review found two
of them defective in the same two ways, which is what three copies of a
subtle algorithm reliably produces.

## The contract

`apportion(total, weights)` returns a share per key such that:

- the shares **sum exactly to `total`** — no cent invented, none lost
- every share has the same sign as `total`
- keys with zero weight receive zero
- it is deterministic: ties break on sorted key order

Exact summation is not fussiness. A department split that loses a cent produces
a Schedule 14 that does not tie, and a discrepancy that looks exactly like a
rounding artefact is indistinguishable from a real bug at audit time.

## What the copies got wrong

**Negative amounts invented money.** `ROUND_DOWN` truncates toward zero, so for a
negative total the floored parts sum to MORE than the total, the shortfall is
negative, and the remainder loop never runs: -100.00 split 6:5 summed to -99.99.
Any correction, void, or clawback line broke the tie-out. This implementation
works on magnitude and reapplies the sign.

**Sub-cent totals silently vanished.** 8.125 split evenly gave 4.06 + 4.06 =
8.12. Unreachable today because overtime output is 2dp, but nothing asserted it
and `line.gross` comes from a provider adapter free to return 3dp. Now an
explicit error rather than a quiet loss.
"""

from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal
from typing import TypeVar

K = TypeVar("K")

_ZERO = Decimal("0")


class InexactApportionError(ValueError):
    """`total` carries more precision than `quantum` can represent exactly.

    Refused rather than rounded away: the caller believes these parts sum to
    `total`, and silently dropping the excess makes that false.
    """


def apportion(
    total: Decimal, weights: Mapping[K, int | Decimal], *, quantum: Decimal
) -> dict[K, Decimal]:
    """Split `total` across `weights`, summing EXACTLY to `total`.

    `quantum` is the smallest representable unit — `Decimal("0.01")` for money
    and for hours-to-the-cent.
    """
    keys = sorted(weights, key=str)
    if not keys:
        return {}

    basis = sum(weights.values())
    if basis <= 0:
        # No weight anywhere: there is no defensible split. Give everything to
        # nobody rather than inventing a distribution.
        return dict.fromkeys(keys, _ZERO)

    if total == _ZERO:
        return dict.fromkeys(keys, _ZERO)

    if total != total.quantize(quantum, rounding=ROUND_DOWN):
        raise InexactApportionError(
            f"total {total} is not an exact multiple of {quantum}; apportioning "
            "it would silently discard the remainder"
        )

    # Work on magnitude so ROUND_DOWN always truncates AWAY from the target,
    # leaving a non-negative shortfall for the remainder pass. Truncating a
    # negative toward zero overshoots and the shortfall goes negative, which is
    # how the previous implementations lost a cent on clawbacks.
    sign = Decimal(-1) if total < _ZERO else Decimal(1)
    magnitude = abs(total)

    exact = {k: magnitude * Decimal(weights[k]) / Decimal(basis) for k in keys}
    shares = {k: v.quantize(quantum, rounding=ROUND_DOWN) for k, v in exact.items()}

    shortfall = magnitude - sum(shares.values())
    steps = int((shortfall / quantum).to_integral_value())
    if steps > 0:
        # Largest fractional remainder first; sorted key order breaks ties so the
        # result is reproducible run to run.
        order = sorted(keys, key=lambda k: (-(exact[k] - shares[k]), str(k)))
        for key in order[:steps]:
            shares[key] += quantum

    result = {k: v * sign for k, v in shares.items()}
    assert sum(result.values()) == total, (
        f"apportion lost money: {sum(result.values())} != {total}"
    )
    return result
