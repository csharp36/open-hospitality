# Contributing to Open Hospitality

Thanks for your interest. This document covers how to propose changes, how to set
up the project, and the ground rules that keep a financial system trustworthy.

## Before you write code

**Small fix or docs?** Open a PR directly.

**A feature or a behavior change?** Start a conversation first — open a
[Discussion in Ideas](../../discussions/categories/ideas) or a
[feature request issue](../../issues/new/choose). We may already have it on the
[roadmap](../../discussions/categories/roadmap); if so we'll link it and close the
duplicate rather than have you build against a plan that's about to change. This is
also enforced by an automated triage bot, so filing first saves you rework.

**Found a security issue?** Do **not** open a public issue or PR. Follow
[SECURITY.md](SECURITY.md).

## The Contributor License Agreement (required)

Every contributor must sign our [Contributor License Agreement](CLA.md) before a pull
request can be merged. It is a **license grant, not a copyright assignment** — you keep
ownership of your work and grant the project the right to use and relicense it. On your
first PR, the **CLA bot** posts a link; sign in one click and it remembers you for
future contributions. A PR cannot merge until the CLA check is green.

## Who can merge

Anyone may open a pull request. Merges are performed by **vetted maintainers** listed in
[CODEOWNERS](.github/CODEOWNERS). New maintainers earn merge rights over time through a
track record of good contributions — an open, earned-committer model. If your PR sits
without review, it's fine to @-mention a maintainer.

## Development setup

**Prerequisites:** Docker, [uv](https://docs.astral.sh/uv/), Node 20+.

```bash
uv sync --extra dev                  # Python deps + test tooling
cd frontend && npm install && cd ..  # frontend deps
scripts/dev.sh start                 # full stack in dependency order
```

Three things a first-time setup must know — none are bugs, all are load-bearing:

1. **The `usali_app` database role must exist before migrations run.** It is a
   cluster-level role that lives outside the migration chain by design; the migration
   *refuses loudly* if it's missing. `scripts/dev.sh start` re-applies
   `scripts/dev_pg_init.sql` idempotently, so an existing database volume converges on
   its own. If you migrate by hand against a pre-existing volume, apply that SQL once.
2. **Keycloak must be provisioned with Organizations enabled.** Login is identity-first
   (username, then password). If you recreate the realm, dev users get new subject ids,
   so `role_assignment.keycloak_subject` needs re-linking.
3. **Tests spin up their own Postgres** via testcontainers (Docker must be running) and
   create the app role themselves — no manual DB setup for the suite.

## Tests and quality gates

Every change ships with tests, and the bar is high because this is money and PII:

```bash
uv run pytest                     # backend
uv run mypy --strict src          # types (strict, src only)
uv run ruff check                 # lint
cd frontend && npm test && npx tsc --noEmit && npx oxlint  # frontend
```

- **Test-driven.** Write the failing test first; it should fail for the *right* reason
  before you make it pass.
- **Tenancy and disclosure are tested at the database**, on the RLS-bound app role — not
  just at the ORM. A change that touches who-can-see-what must prove the wall holds
  against a cross-tenant read, not merely that the happy path works.
- **No differencing oracles, no disclosure-direction regressions.** Aggregates that
  suppress small groups (to protect an individual's rate) must stay suppressed under a
  caller-chosen window.

## Commits and pull requests

- **One concern per commit.** History is part of the review — we merge with a **merge
  commit or rebase, never squash**, so keep commits atomic and legible.
- Reference the issue or discussion your PR addresses.
- Fill out the PR template; the checklist is there to protect you.
- CI (tests, types, lint) and the CLA check must be green to merge.

## Code of Conduct

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md). Be kind; assume
good faith.
