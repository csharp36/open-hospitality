"""The ONE definition of a legal deposit-allocation chain (E5).

Consumed by two doors that must never drift: the vault API refuses a bad
chain at entry (422), and pay-run preflight refuses to send one that got in
anyway (a named blocker). Money that sums to 99% is silently lost and 101%
is a provider rejection, so the rule is structural: fixed `amount` rows
(dollars > 0) and `percent` rows (of net, exclusive 0..100, summing < 100)
carve pieces off, and EXACTLY ONE `remainder` row — always LAST — receives
everything left. "Where does the rest go" is therefore always answered.

This function sees ONLY allocation metadata. Account and routing values are
never passed in, so no refusal message can leak one — and the messages name
the violated rule, never the caller's own figures (the C2 lesson: field
echoes travel to logs and clients).
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.models import DepositAccount

ALLOCATION_TYPES = frozenset({"amount", "percent", "remainder"})
_HUNDRED = Decimal("100")


def account_slot(ordinal: int, legacy: bool) -> str:
    """The aad slot label for a row's account envelope (the full aad is
    `{employee_id}:{slot}`). Derived from ROW IDENTITY, never stored — that is
    what makes it a binding: an envelope moved to another ordinal, field, or
    employee derives a different aad and FAILS to open. Backfilled rows keep
    the pre-E5 slot they were actually sealed under; the migration cannot
    re-seal. The frontend's sealing helper mirrors these strings — a
    round-trip test pins the contract."""
    return "bank_account" if legacy else f"deposit:{ordinal}:account"


def routing_slot(ordinal: int, legacy: bool) -> str:
    return "bank_routing" if legacy else f"deposit:{ordinal}:routing"


def deposit_problems(session: Session, employee_id: int, who: str) -> list[str]:
    """Preflight's view of one employee's chain: required, complete, legal.

    Runs `allocation_violation` AGAIN even though the API door already did —
    defence in depth, same function, so the doors cannot drift, and the schema
    alone cannot express every illegality (a no-remainder chain is insertable
    row by row). Messages name slots and ordinals, never values."""
    rows = list(session.execute(
        select(DepositAccount)
        .where(DepositAccount.employee_id == employee_id)
        .order_by(DepositAccount.ordinal)
    ).scalars())
    if not rows:
        return [f"{who}: no deposit accounts on file"]
    problems = []
    for r in rows:
        # '' is the e5a0 backfill's never-sealed placeholder: not on file,
        # exactly as the missing COLUMN was before E5.
        missing = [
            name for name, value in
            (("account", r.sealed_account), ("routing", r.sealed_routing))
            if value == ""
        ]
        if missing:
            problems.append(
                f"{who}: deposit account {r.ordinal} missing {', '.join(missing)}"
            )
        if r.account_type is None:
            # Only backfilled rows can hold NULL (the API requires a type on
            # every new write): the pre-E5 profile never said checking or
            # savings, and the ACH account class must not be guessed.
            problems.append(
                f"{who}: deposit account {r.ordinal} has no account type on "
                "file -- checking or savings was never stated; set it before "
                "this destination can be sent to the provider"
            )
    violation = allocation_violation([
        AllocationSpec(allocation_type=r.allocation_type,
                       allocation_value=r.allocation_value)
        for r in rows
    ])
    if violation is not None:
        problems.append(f"{who}: deposit allocation invalid -- {violation}")
    return problems


@dataclass(frozen=True)
class AllocationSpec:
    allocation_type: str
    allocation_value: Decimal | None


def allocation_violation(specs: list[AllocationSpec]) -> str | None:
    """None if `specs` (in ordinal order) is a legal chain, else the refusal.

    An EMPTY chain is legal here: whether deposit data is REQUIRED at all is
    preflight's per-employee completeness question, not an allocation shape.
    """
    if not specs:
        return None
    for s in specs:
        if s.allocation_type not in ALLOCATION_TYPES:
            return (
                f"allocation_type must be one of {sorted(ALLOCATION_TYPES)} "
                "(value withheld from this message)"
            )
        if (s.allocation_type == "remainder") != (s.allocation_value is None):
            return (
                "a remainder account carries no allocation_value, and an "
                "amount or percent account requires one"
            )
    remainders = [s for s in specs if s.allocation_type == "remainder"]
    if len(remainders) != 1:
        return (
            "a chain has exactly one remainder account -- it is the answer "
            "to 'where does the rest of the paycheck go'"
        )
    if specs[-1].allocation_type != "remainder":
        return "the remainder account must be the last in the chain"
    for s in specs:
        if s.allocation_type == "amount":
            assert s.allocation_value is not None  # narrowed above
            if s.allocation_value <= 0:
                return "a fixed amount must be greater than zero"
        elif s.allocation_type == "percent":
            assert s.allocation_value is not None  # narrowed above
            if not (Decimal("0") < s.allocation_value < _HUNDRED):
                return "a percent must be between 0 and 100, exclusive"
    percent_sum = sum(
        (s.allocation_value for s in specs
         if s.allocation_type == "percent" and s.allocation_value is not None),
        Decimal("0"),
    )
    if percent_sum >= _HUNDRED:
        return (
            "percent allocations must sum to less than 100 -- at 100 or more "
            "the remainder account is starved by construction"
        )
    return None
