"""Supply-chain invariants for code executed by GitHub Actions."""

import re
from pathlib import Path


_WORKFLOWS = Path(".github/workflows")
_REMOTE_ACTION = re.compile(r"^\s*-?\s*uses:\s*(?!\./)([^@\s]+)@([^#\s]+)", re.MULTILINE)
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def test_remote_actions_are_pinned_to_full_commit_shas():
    mutable: list[str] = []
    for workflow in sorted(_WORKFLOWS.glob("*.yml")):
        for action, ref in _REMOTE_ACTION.findall(workflow.read_text()):
            if _FULL_SHA.fullmatch(ref) is None:
                mutable.append(f"{workflow}: {action}@{ref}")

    assert mutable == [], "mutable action references:\n" + "\n".join(mutable)


def test_secret_bearing_triage_install_requires_hashes():
    workflow = (_WORKFLOWS / "triage.yml").read_text()
    assert "pip install --quiet --require-hashes -r .github/triage-requirements.txt" in workflow
