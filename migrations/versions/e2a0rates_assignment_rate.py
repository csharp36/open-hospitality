"""Effective-dated rates per placement, backfilled from `Employee.pay_rate`.

A rate belongs to an assignment and a date range, not to a person. The scalar it
replaces had no dating, so re-promoting a closed period after a raise re-costed
already-worked hours: an April period costing $160 became $240 because a raise
dated 1 August was entered. `tests/test_closed_period_stability.py` pins that.

## Backfill

One `regular` row per assignment, from the employee's current `pay_rate`.

`effective_from = GREATEST(a.effective_from, ANCHOR)`, bounded above by the
assignment's own `effective_to`. It keys off the ASSIGNMENT's start, not the
employee's `hire_date`, and that is deliberate: `e1i0` already moved assignment
starts forward to respect hire dates, so deriving from `hire_date` again would
be a second copy of that rule, free to drift from the first. A rate also cannot
sensibly precede the placement it prices. One source of truth, which is the
lesson E1 spent three reviews learning.

NO HISTORY IS IMPORTED, deliberately. The pilot's prior rate history exists only
in the incumbent's export on an encrypted volume; importing real compensation
data to reconstruct history we were never asked for is not worth the exposure.
So every assignment gets exactly one rate row, and dates before it resolve to
NOTHING -- `rate_on` returns None and callers must treat that as "cannot cost",
never as zero. Zero is a priced value and would enter the suppression population
as real cost, disclosing a solo department.

`ot`/`dot`/`holiday` are NOT backfilled. There is no source for them: the
current engine derives OT and DT from the multipliers in `overtime_rules.py`,
and nothing has ever recorded a holiday rate. Absent means "use the
jurisdiction rule", which is exactly today's behaviour, so the backfill changes
no number.
"""

from alembic import op
import sqlalchemy as sa

revision = "e2a0rates"
down_revision = "e1i0hiredate"
branch_labels = None
depends_on = None

_ANCHOR = "2026-01-05"


def upgrade() -> None:
    op.create_table(
        "assignment_rate",
        sa.Column("assignment_rate_id", sa.Integer(), autoincrement=True,
                  nullable=False),
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("rate_type", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("assignment_rate_id"),
        sa.ForeignKeyConstraint(["assignment_id"],
                                ["employee_assignment.assignment_id"]),
        sa.UniqueConstraint("assignment_id", "rate_type", "effective_from"),
    )
    op.create_index(
        "ix_assignment_rate_lookup", "assignment_rate",
        ["assignment_id", "rate_type", "effective_from"],
    )

    # ONE OPEN RATE per (assignment, rate_type). Partial, scoped to open rows
    # only -- the same shape and the same reason as
    # `uq_one_active_primary_per_employee`: the ordinary raise is "close the old
    # rate, open the new one", which leaves a CLOSED row and an OPEN row for the
    # same pair and must stay legal. Only two SIMULTANEOUSLY-open rates are
    # forbidden, because that is a genuine ambiguity about what someone is paid.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_one_open_rate_per_assignment_type
            ON assignment_rate (assignment_id, rate_type)
         WHERE effective_to IS NULL
        """
    )

    # Backfill `regular` only. GREATEST(a.effective_from, ANCHOR) so a rate
    # cannot precede the placement it prices -- and since e1i0 moved assignment
    # starts forward to respect hire_date, that guard is inherited rather than
    # duplicated. LEAST(..., effective_to) so it cannot start after the
    # assignment has ended, which would be an interval no date predicate can
    # reason about.
    op.execute(
        f"""
        INSERT INTO assignment_rate
               (assignment_id, rate_type, amount, effective_from, effective_to)
        SELECT a.assignment_id,
               'regular',
               e.pay_rate,
               CASE
                 WHEN a.effective_to IS NULL
                   THEN GREATEST(a.effective_from, DATE '{_ANCHOR}')
                 ELSE LEAST(
                        GREATEST(a.effective_from, DATE '{_ANCHOR}'),
                        a.effective_to
                      )
               END,
               a.effective_to
          FROM employee_assignment a
          JOIN employee e ON e.employee_id = a.employee_id
         WHERE e.pay_rate IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("uq_one_open_rate_per_assignment_type",
                  table_name="assignment_rate")
    op.drop_index("ix_assignment_rate_lookup", table_name="assignment_rate")
    # Rate HISTORY is lost, not merely the table: `Employee.pay_rate` holds one
    # scalar and cannot express the ranges this table records. Any raise entered
    # after this migration is unrecoverable on downgrade. Stated plainly because
    # a downgrade that quietly discards payroll history is worse than one that
    # refuses.
    op.drop_table("assignment_rate")
