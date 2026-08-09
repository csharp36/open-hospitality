"""I2: `wage_settlement` — the missing terminal resolution, created empty.

One row per settlement ACT (decision 3): a named worked-hours delta on a
submitted run, paid OUTSIDE the integration and acknowledged by an
authorized actor. Created EMPTY, deliberately — no settlements exist
historically (the mechanism is new with this pillar), so there is nothing
to backfill and inventing rows would fabricate money history.

The sign CHECK is the semantics: hours > 0. A negative settlement would
UN-PAY someone (the guard subtracts the settled sum), and a ZERO one would
record an act that had no effect — the F6 acknowledgment rule; the
endpoint refuses zero deltas and the schema refuses them independently.
FKs to pay_run and employee refuse a settlement against a run or person
that never existed. NO unique constraint on (pay_run_id, employee_id):
several settlements per pair are legal by design — a post-settlement punch
re-blocks with the residual only, and the residual settles as its own act.
`note` is bounded operator free text (the TimecardAdjustment.reason
posture), audit-surface only.

Downgrade drops the table — and with it any recorded settlements. That is
the only shape the pre-I schema can express, and the failure direction is
LOUD either way (the I6 migration lens made this precise): post-I code
still running against the downgraded schema fails EVERY preflight and
execute on the missing table — nothing can submit — and once the code is
rolled back too, the worked-hours guard re-raises exactly the blockers
those settlements had cleared. Neither direction silently forgets unpaid
hours. Deploy order for the UPGRADE is the repo standard, migrate before
deploy: new code against the old schema hits the same missing table on
every preflight (loud, fail-closed, recorded in the backlog).
"""

from alembic import op
import sqlalchemy as sa

revision = "i2a0settle"
down_revision = "h2a0wagepath"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wage_settlement",
        sa.Column("settlement_id", sa.Integer, primary_key=True,
                  autoincrement=True),
        sa.Column("pay_run_id", sa.Integer,
                  sa.ForeignKey("pay_run.pay_run_id"), nullable=False),
        sa.Column("employee_id", sa.Integer,
                  sa.ForeignKey("employee.employee_id"), nullable=False),
        sa.Column("hours", sa.Numeric(10, 2), nullable=False),
        sa.Column("actor_subject", sa.String(64), nullable=False),
        sa.Column("note", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("hours > 0",
                           name="ck_wage_settlement_hours_positive"),
    )


def downgrade() -> None:
    op.drop_table("wage_settlement")
