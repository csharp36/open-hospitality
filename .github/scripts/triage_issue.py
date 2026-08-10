#!/usr/bin/env python3
"""Triage a newly-opened feature request against the roadmap and open issues.

Fires from .github/workflows/triage.yml on `issues: opened`. It asks Claude
whether the new request is already covered by an entry in .github/roadmap.yml
or an existing open feature issue. If it is (with high confidence), the bot
comments with the match, labels the issue, and closes it as a duplicate —
always with an appeal line so a human can reopen a bad call. Otherwise it just
labels the issue `needs-triage` and leaves it open for a maintainer.

Design notes:
  * roadmap.yml is the single source of truth for "already planned". Keeping
    the canonical backlog in one machine-readable file is what lets the bot
    dedup reliably — don't split it across issues and this file.
  * The model is a config knob (TRIAGE_MODEL). Default is Haiku 4.5: this is a
    high-volume, simple classification task, which is exactly what Haiku is
    for. Set TRIAGE_MODEL=claude-opus-4-8 for maximum judgment at higher cost.
  * Fail SAFE. Any error, or confidence below threshold, leaves the issue OPEN
    and labeled `needs-triage`. The bot never hard-closes without a comment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import yaml
from pydantic import BaseModel

MODEL = os.environ.get("TRIAGE_MODEL", "claude-haiku-4-5")
THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.8"))
REPO = os.environ["GITHUB_REPOSITORY"]
ISSUE = os.environ["ISSUE_NUMBER"]
ROADMAP_PATH = ".github/roadmap.yml"
MAX_OPEN_ISSUES = 60  # how many recent open feature issues to compare against


def gh(*args: str, check: bool = True) -> str:
    """Run a gh CLI command and return stdout. gh authenticates via GH_TOKEN."""
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=check
    )
    return result.stdout.strip()


class Verdict(BaseModel):
    """The model's dedup decision. `match_ref` is a roadmap id (e.g. 'OH-1') or
    an issue number as a string; empty when there's no match."""

    is_duplicate: bool
    match_kind: str  # "roadmap" | "issue" | "none"
    match_ref: str
    match_title: str
    confidence: float
    rationale: str


SYSTEM = """\
You are the issue-triage assistant for an open-source project. Your one job is
to decide whether a NEW feature request is already covered by an existing
roadmap entry or an existing open feature issue.

Be conservative. Mark it a duplicate ONLY when the new request is asking for the
same underlying capability as a candidate — not merely the same general area.
Two requests about "scheduling" that ask for different things are NOT duplicates.
When unsure, say it is not a duplicate (is_duplicate=false, match_kind="none").

Pick the single best match. Set match_kind to "roadmap" (and match_ref to the
roadmap id like "OH-3") or "issue" (and match_ref to the issue number). Set
confidence in [0,1] for how sure you are it's the same request. Keep rationale
to one or two sentences a maintainer can skim."""


def load_roadmap() -> list[dict]:
    if not os.path.exists(ROADMAP_PATH):
        return []
    with open(ROADMAP_PATH) as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("roadmap", [])


def open_feature_issues() -> list[dict]:
    """Recent open issues labeled 'enhancement', excluding the current one."""
    raw = gh(
        "issue", "list", "--repo", REPO, "--state", "open",
        "--label", "enhancement", "--limit", str(MAX_OPEN_ISSUES),
        "--json", "number,title",
    )
    issues = json.loads(raw) if raw else []
    return [i for i in issues if str(i["number"]) != str(ISSUE)]


def build_prompt(issue: dict, roadmap: list[dict], issues: list[dict]) -> str:
    roadmap_block = "\n".join(
        f"- [{e['id']}] {e['title']}: {e.get('summary', '').strip()}"
        for e in roadmap
    ) or "(none)"
    issues_block = "\n".join(
        f"- [#{i['number']}] {i['title']}" for i in issues
    ) or "(none)"
    return (
        f"NEW REQUEST (issue #{issue['number']}):\n"
        f"Title: {issue['title']}\n"
        f"Body:\n{(issue.get('body') or '').strip()[:4000]}\n\n"
        f"ROADMAP ENTRIES:\n{roadmap_block}\n\n"
        f"OPEN FEATURE ISSUES:\n{issues_block}\n\n"
        f"Is the NEW REQUEST already covered by one of the candidates above?"
    )


def label(name: str) -> None:
    gh("issue", "edit", ISSUE, "--repo", REPO, "--add-label", name, check=False)


def comment(body: str) -> None:
    gh("issue", "comment", ISSUE, "--repo", REPO, "--body", body, check=False)


def close_as_duplicate() -> None:
    gh("issue", "close", ISSUE, "--repo", REPO, "--reason", "not planned", check=False)


APPEAL = (
    "\n\n---\n_Automated triage. If this **isn't** the same request, reply here "
    "and a maintainer will reopen it — nothing is lost._"
)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set — skipping triage (add it as a repo secret).")
        return 0

    issue = json.loads(
        gh("issue", "view", ISSUE, "--repo", REPO, "--json", "number,title,body")
    )
    roadmap = load_roadmap()
    issues = open_feature_issues()

    # Import here so a missing key above exits before we need the SDK.
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": build_prompt(issue, roadmap, issues)}],
        output_format=Verdict,
    )
    v: Verdict = resp.parsed_output
    print(f"verdict: {v.model_dump_json()}")

    if v.is_duplicate and v.match_kind in ("roadmap", "issue") and v.confidence >= THRESHOLD:
        if v.match_kind == "roadmap":
            label("on-roadmap")
            comment(
                f"Thanks! This is already on our roadmap as "
                f"**{v.match_ref} — {v.match_title}**. {v.rationale}\n\n"
                f"Closing as a duplicate so the discussion stays in one place — "
                f"follow the [roadmap](../../discussions/categories/roadmap) for "
                f"progress." + APPEAL
            )
        else:  # issue
            label("duplicate")
            comment(
                f"Thanks! This looks like a duplicate of #{v.match_ref} "
                f"(**{v.match_title}**). {v.rationale}\n\n"
                f"Closing so the conversation stays in one thread — please add "
                f"anything new over on #{v.match_ref}." + APPEAL
            )
        close_as_duplicate()
        print(f"closed #{ISSUE} as duplicate of {v.match_kind} {v.match_ref}")
    else:
        label("needs-triage")
        print(f"left #{ISSUE} open for human triage")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail safe: never hard-close on an error
        print(f"triage error: {exc}", file=sys.stderr)
        label("needs-triage")
        sys.exit(0)
