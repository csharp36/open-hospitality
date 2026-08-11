import ast
from pathlib import Path

import pytest

from usali.invariants import InvariantViolation, require, require_not_none


def test_require_raises_an_explicit_runtime_error():
    with pytest.raises(InvariantViolation, match="money did not tie"):
        require(False, "money did not tie")


def test_require_not_none_preserves_the_narrowed_value():
    assert require_not_none("value", "missing") == "value"
    with pytest.raises(InvariantViolation, match="missing"):
        require_not_none(None, "missing")


@pytest.mark.parametrize(
    "module",
    ["apportion.py", "qbo_push.py", "payroll_run.py"],
)
def test_financial_modules_do_not_use_optimization_sensitive_asserts(module):
    tree = ast.parse((Path("src/usali") / module).read_text())
    assert not any(isinstance(node, ast.Assert) for node in ast.walk(tree))
