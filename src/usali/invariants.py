"""Runtime invariant checks that remain active under ``python -O``."""

from typing import TypeVar


T = TypeVar("T")


class InvariantViolation(RuntimeError):
    """Internal state contradicted a condition established earlier."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InvariantViolation(message)


def require_not_none(value: T | None, message: str) -> T:
    if value is None:
        raise InvariantViolation(message)
    return value
