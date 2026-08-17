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
        ("pip", "/.github"),
        ("npm", "/frontend"),
        ("docker", "/"),
        ("github-actions", "/"),
    }


def test_codeql_actions_are_pinned_to_full_commit_shas() -> None:
    lines = (ROOT / ".github/workflows/codeql.yml").read_text().splitlines()
    action_lines = [line for line in lines if line.lstrip().startswith("uses:")]

    assert action_lines
    assert all(FULL_SHA_ACTION.match(line) for line in action_lines)


def test_codeql_scans_both_backend_and_frontend_languages() -> None:
    # The PR's headline claim -- CodeQL covers Python AND JS/TS. Pin the exact
    # matrix set so dropping (or adding) a language fails in both directions;
    # the pinning test above cannot observe the matrix at all.
    config = yaml.safe_load((ROOT / ".github/workflows/codeql.yml").read_text())
    languages = config["jobs"]["analyze"]["strategy"]["matrix"]["language"]

    assert set(languages) == {"python", "javascript-typescript"}


def test_codeql_runs_on_push_pr_and_schedule() -> None:
    # yaml parses the bare `on:` key as the boolean True, so read it back that way.
    config = yaml.safe_load((ROOT / ".github/workflows/codeql.yml").read_text())
    triggers = config[True]

    assert {"push", "pull_request", "schedule"} <= set(triggers)
