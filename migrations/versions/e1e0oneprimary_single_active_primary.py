"""e1: at most one ACTIVE primary assignment per employee

The double-payment guarantee rests on an employee having exactly one primary
assignment: the pay-run population keys on it, so one timecard reaches one run.
Nothing enforced that. The table's unique key is
(employee_id, property_id, effective_from), which happily permits primaries at
two DIFFERENT properties -- and an adversarial review reproduced the result:
$500 submitted to the payroll provider where $250 was owed.

It held only because the backfill was the sole writer. The first admin UI that
adds an assignment would have broken it silently.

A partial unique index makes the state unreachable rather than merely checked.
It is deliberately narrow -- `is_primary AND status='active'` -- so history
(closed or deactivated primaries from past transfers) is unaffected; only
CONCURRENT active primaries are forbidden.

SCOPE: the index covers only OPEN primaries (`effective_to IS NULL`). That is
what makes it compatible with the ordinary transfer pattern -- close the old
assignment by setting effective_to, open the new one -- which a first attempt at
this index wrongly forbade. History is untouched; only two simultaneously OPEN
active primaries are impossible.

It is therefore necessary but NOT sufficient: two primaries could still overlap
if one carries a far-future effective_to. That residue is caught on the read
side by employee_ids_with_primary_at, which routes every candidate through
primary_assignment_on and raises AmbiguousPrimaryError BEFORE any provider call.
Belt and braces, deliberately -- this invariant is worth $250 per occurrence.

Revision ID: e1e0oneprimary
Revises: e1d0actualkey
"""

from alembic import op

revision = "e1e0oneprimary"
down_revision = "e1d0actualkey"
branch_labels = None
depends_on = None

_INDEX = "uq_one_active_primary_per_employee"


def upgrade() -> None:
    # Defensive: demote any pre-existing duplicates to non-primary rather than
    # failing the migration. Keeps the lowest assignment_id as the primary --
    # deterministic, and the operator can correct it afterwards.
    op.execute(
        """
        UPDATE employee_assignment SET is_primary = false
        WHERE is_primary AND status = 'active' AND effective_to IS NULL
          AND assignment_id NOT IN (
            SELECT MIN(assignment_id) FROM employee_assignment
            WHERE is_primary AND status = 'active' AND effective_to IS NULL
            GROUP BY employee_id
        )
        """
    )
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_INDEX}
        ON employee_assignment (employee_id)
        WHERE is_primary AND status = 'active' AND effective_to IS NULL
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
