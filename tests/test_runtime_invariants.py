import ast
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from usali import apportion as apportion_mod
from usali.apportion import apportion
from usali.invariants import InvariantViolation, require, require_not_none


def test_require_raises_an_explicit_runtime_error():
    with pytest.raises(InvariantViolation, match="money did not tie"):
        require(False, "money did not tie")


def test_apportion_tie_out_invariant_fails_closed_on_a_broken_split(monkeypatch):
    # The tie-out `require` in apportion is a can't-happen net over a correct
    # algorithm, so no ordinary input reaches it. Corrupt the magnitude (via the
    # module-global `abs` lookup) so the shares no longer sum to `total`, and the
    # guard must RAISE rather than return money that does not tie. This is the
    # test that kills the "delete the require block" / "require(True, ...)"
    # mutants -- without a live guard, apportion silently returns wrong money.
    monkeypatch.setattr(
        apportion_mod, "abs", lambda value: value + Decimal("1"), raising=False
    )
    with pytest.raises(InvariantViolation, match="apportion lost money"):
        apportion(Decimal("10.00"), {"a": 1, "b": 1}, quantum=Decimal("0.01"))


def test_invariants_survive_python_dash_O():
    # The whole point of issue #36: `assert` is stripped under `python -O`, so a
    # money guard written as `assert` silently vanishes in an optimized run.
    # Prove empirically that `require` still raises under -O while a bare
    # `assert` does not -- the AST scan only checks the keyword is absent today,
    # it cannot prove the replacement actually fires when optimized.
    script = (
        "import sys\n"
        "assert sys.flags.optimize == 2, 'test harness must run under -O'\n"
        "stripped = True\n"
        "try:\n"
        "    assert False, 'this assert is stripped under -O'\n"
        "except AssertionError:\n"
        "    stripped = False\n"
        "assert stripped, 'assert was NOT stripped -- harness is not under -O'\n"
        "from usali.invariants import require, InvariantViolation\n"
        "try:\n"
        "    require(False, 'money did not tie')\n"
        "except InvariantViolation:\n"
        "    print('OK')\n"
        "    sys.exit(0)\n"
        "sys.exit('require did NOT raise under -O')\n"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


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
