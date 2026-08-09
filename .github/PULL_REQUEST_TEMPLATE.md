<!-- Thanks for contributing to Open Hospitality. -->

## What and why

<!-- What does this change do, and what problem does it solve? -->

Closes #<!-- issue number, or link the Discussion this implements -->

## How it was verified

<!-- Tests added/changed, and the commands you ran. -->

- [ ] `uv run pytest` passes
- [ ] `uv run mypy --strict src` clean
- [ ] `uv run ruff check` clean
- [ ] Frontend (if touched): `npm test`, `tsc --noEmit`, `oxlint` clean

## Checklist

- [ ] I signed the [CLA](../CLA.md) (the bot will confirm).
- [ ] Commits are atomic — one concern each (we merge, we don't squash).
- [ ] Tests cover the change and fail without it.
- [ ] If this touches **tenancy, access control, PII, or pay disclosure**, I added a
      test that proves the boundary at the database (RLS-bound), not just the ORM.
- [ ] No secrets, credentials, or real PII in code, tests, or fixtures.
- [ ] Docs updated where behavior changed.

## Anything reviewers should know

<!-- Trade-offs, follow-ups, decisions you'd like a second opinion on. -->
