"""E4: the sick-leave ledger. Created EMPTY, deliberately.

There is no backfill because there is nothing trustworthy to backfill FROM:
the incumbent's `CA Sick Time Policy` balances may be running the pre-2024
24/48-hour ruleset (docs/reference/ca-sick-leave.md), and inventing statutory
history would be the invented-'checking' mistake at larger stakes -- these
hours are a legal entitlement. The pilot's real opening balances enter as
AUDITED human `adjustment` entries through the payroll-gated API, one per
employee, read off the incumbent's final statement by a person who can vouch
for them. Recorded here so the empty table is understood as a decision, not
an omission.

The sign CHECK is the semantics: accrual rows positive, usage rows negative,
adjustments either way but never zero. A positive usage row would silently
GRANT hours.
"""

from alembic import op
import sqlalchemy as sa

revision = "e4a0sick"
down_revision = "e5b0dropbank"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sick_leave_ledger",
        sa.Column("entry_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(),
                  sa.ForeignKey("employee.employee_id"), nullable=False),
        sa.Column("entry_type", sa.String(10), nullable=False),
        sa.Column("hours", sa.Numeric(6, 2), nullable=False),
        # The accrual cap in force when a POSITIVE entry happened -- recorded
        # as a fact so the balance fold clamps history at ITS OWN caps, never
        # retroactively at today's (the E4 review's Critical: a decaying day
        # length otherwise extinguishes lawfully banked hours). NULL = no
        # clamp (negative entries; no day-length data; no mandate).
        sa.Column("cap_hours", sa.Numeric(6, 2), nullable=True),
        sa.Column("effective_on", sa.Date(), nullable=False),
        sa.Column("timecard_id", sa.Integer(),
                  sa.ForeignKey("timecard.timecard_id"), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(entry_type = 'accrual' AND hours > 0) OR "
            "(entry_type = 'usage' AND hours < 0) OR "
            "(entry_type = 'usage_void' AND hours > 0) OR "
            "(entry_type = 'adjustment' AND hours <> 0)",
            name="ck_sick_ledger_sign_matches_type",
        ),
    )
    op.create_index(
        "ix_sick_leave_ledger_employee", "sick_leave_ledger", ["employee_id"]
    )
    # A client retry must not double-book sick hours: identical usage for one
    # employee-day is refused (the punch-debounce posture).
    op.create_index(
        "uq_sick_usage_dedup", "sick_leave_ledger",
        ["employee_id", "effective_on", "hours"],
        unique=True, postgresql_where=sa.text("entry_type = 'usage'"),
    )


def downgrade() -> None:
    # Drops REAL accrual history that cannot be rebuilt (accruals re-derive
    # from timecards on re-promote, but usage and opening adjustments are
    # human entries with no other home). Same posture as every destructive
    # downgrade in this chain: do not run it after the ledger holds entries
    # you care about.
    op.drop_index("uq_sick_usage_dedup", table_name="sick_leave_ledger")
    op.drop_index("ix_sick_leave_ledger_employee", table_name="sick_leave_ledger")
    op.drop_table("sick_leave_ledger")
