from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
FULL_SHA_ACTION = re.compile(r"^\s*uses:\s*[^\s@]+@[0-9a-f]{40}(?:\s+#.*)?$")


def test_dependabot_covers_every_repository_package_ecosystem() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())

    configured = {
        (update["package-ecosystem"], update["directory"])
        for update in config["updates"]
    }
    assert configured == {
        ("pip", "/"),
        ("npm", "/frontend"),
        ("docker", "/"),
        ("github-actions", "/"),
    }


def test_codeql_actions_are_pinned_to_full_commit_shas() -> None:
    lines = (ROOT / ".github/workflows/codeql.yml").read_text().splitlines()
    action_lines = [line for line in lines if line.lstrip().startswith("uses:")]

    assert action_lines
    assert all(FULL_SHA_ACTION.match(line) for line in action_lines)
