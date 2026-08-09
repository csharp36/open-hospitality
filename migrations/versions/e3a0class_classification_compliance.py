"""E3: classification and compliance fields on `employee`.

Five columns and one widening:

- `pay_type` String(10) -> String(20): it gains `exclude_from_payroll` (19
  chars) as a third VALUE, not a boolean beside the other two -- a boolean
  would let `hourly` + excluded express a person who is simultaneously priced
  and unpriceable.
- `full_part_time`, `i9_submitted_on`, `w4_submitted_on`: nullable, additive.
- `employment_status`: NOT NULL after backfill. `terminated` where
  `termination_date` is set, `active` otherwise -- the enum REPLACES the
  inference "terminated means termination_date IS NOT NULL", it does not
  replace the date, which schedule_api still compares against. The CHECK
  constraint (added LAST, after the backfill satisfies it) keeps enum and date
  from ever disagreeing.
- `payroll_data_complete`: NOT NULL after backfill, TRUE for every existing
  row. Existing staff are being paid today; a new flag that blocks the pilot's
  next pay run on day one is a false alarm. The MODEL default is False, so
  every post-E3 hire is blocked until a human states their paperwork is in.

No server defaults on the NOT NULL columns (the e1f0 lesson): a new row's
status and completeness must be the application's explicit decision, not the
schema's silent one.
"""

from alembic import op
import sqlalchemy as sa

revision = "e3a0class"
down_revision = "e2b0droprate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "employee", "pay_type",
        existing_type=sa.String(10), type_=sa.String(20),
        existing_nullable=False,
    )
    op.add_column(
        "employee", sa.Column("full_part_time", sa.String(10), nullable=True)
    )
    op.add_column(
        "employee", sa.Column("i9_submitted_on", sa.Date(), nullable=True)
    )
    op.add_column(
        "employee", sa.Column("w4_submitted_on", sa.Date(), nullable=True)
    )
    op.add_column(
        "employee", sa.Column("employment_status", sa.String(10), nullable=True)
    )
    op.add_column(
        "employee",
        sa.Column("payroll_data_complete", sa.Boolean(), nullable=True),
    )

    op.execute(
        "UPDATE employee SET employment_status = "
        "CASE WHEN termination_date IS NOT NULL THEN 'terminated' ELSE 'active' END"
    )
    op.execute("UPDATE employee SET payroll_data_complete = TRUE")

    op.alter_column("employee", "employment_status", nullable=False)
    op.alter_column("employee", "payroll_data_complete", nullable=False)
    # After the backfill, so existing rows already satisfy it.
    op.create_check_constraint(
        "ck_employee_terminated_iff_dated",
        "employee",
        "(employment_status = 'terminated') = (termination_date IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_employee_terminated_iff_dated", "employee")
    op.drop_column("employee", "payroll_data_complete")
    op.drop_column("employee", "employment_status")
    op.drop_column("employee", "w4_submitted_on")
    op.drop_column("employee", "i9_submitted_on")
    op.drop_column("employee", "full_part_time")
    # Narrowing REFUSES (Postgres value-too-long) if any row holds the 19-char
    # `exclude_from_payroll`. Correct: truncating it would leave a
    # valid-looking garbage pay type that downstream code would then trust.
    # Reclassify those employees first, or don't downgrade.
    op.alter_column(
        "employee", "pay_type",
        existing_type=sa.String(20), type_=sa.String(10),
        existing_nullable=False,
    )
