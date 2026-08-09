"""Per-property pay-run census, so suppression counts the right population.

The actual-side suppression gate counted `pay_run_line`, which has NO property
dimension and whose `gross` is the employee's entire cross-property paycheck.
Since E1 a paycheck is issued by the employee's primary property while the COST
is apportioned across every property they worked at, so an employee whose
primary is A but who worked the period at B contributed a HEADCOUNT to A and
zero dollars to it. A department where one person actually worked read as two,
escaped suppression, and published that person's exact gross and employer
burden -- from which `gross / hours` re-derives their hourly rate.

`fetch_pay_run_results` already computed this split to write the department
aggregates and discarded it. This table persists it, so the census and the money
are written from the same apportionment in the same transaction.

NO BACKFILL IS POSSIBLE, and that is deliberate. The split depended on
kiosk-derived hours per property at fetch time; reconstructing it now would be a
guess, and a guess here silently changes which departments disclose money. Any
pay run processed before this migration therefore has an EMPTY census, which
reads as zero priced employees and SUPPRESSES. Failing closed is the correct
direction for a disclosure gate: an over-suppressed report is a nuisance, an
under-suppressed one is an unrecoverable disclosure. Re-run
`fetch_pay_run_results` for those runs to repopulate.
"""

from alembic import op
import sqlalchemy as sa

revision = "e1h0census"
down_revision = "e1g0dropcols"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Jurisdiction becomes NULLABLE. `e1f0nodefault` removed the server default
    # so a property could not silently inherit California overtime, but the ORM
    # carried a Python-side default that defeated it. Removing that exposed the
    # real gap: PMS ingestion creates properties, and a revenue file knows
    # nothing about wage law. NULL is the honest "nobody has said yet";
    # `rules_for(None)` refuses, so an unstated jurisdiction blocks a pay run by
    # name instead of costing every hour as Californian.
    op.alter_column("property", "wage_jurisdiction", nullable=True,
                    existing_type=sa.String(length=10))

    op.create_table(
        "pay_run_line_property",
        sa.Column("pay_run_line_property_id", sa.BigInteger(), autoincrement=True,
                  nullable=False),
        sa.Column("pay_run_id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("hours", sa.Numeric(10, 2), nullable=False),
        sa.Column("gross", sa.Numeric(15, 4), nullable=False),
        sa.Column("employer_burden", sa.Numeric(15, 4), nullable=False),
        sa.PrimaryKeyConstraint("pay_run_line_property_id"),
        sa.ForeignKeyConstraint(["pay_run_id"], ["pay_run.pay_run_id"]),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.employee_id"]),
        sa.ForeignKeyConstraint(["department_id"], ["department.department_id"]),
        # One row per employee per property per run. A duplicate would double a
        # headcount and could un-suppress a solo department on its own.
        sa.UniqueConstraint("pay_run_id", "employee_id", "property_id"),
    )
    # The gate reads by (run, property); without this it is a full scan of every
    # census row ever written on the SOS path.
    op.create_index(
        "ix_pay_run_line_property_run_property",
        "pay_run_line_property",
        ["pay_run_id", "property_id"],
    )


def downgrade() -> None:
    # Dropping this table reverts the suppression gate to counting `pay_run_line`
    # -- i.e. reintroduces the disclosure. Only safe alongside reverting the
    # reporting code that reads it.
    op.drop_index("ix_pay_run_line_property_run_property",
                  table_name="pay_run_line_property")
    op.drop_table("pay_run_line_property")
    # Cannot restore NOT NULL faithfully: rows created while it was nullable may
    # legitimately hold NULL, and inventing a jurisdiction for them is the exact
    # silent-default this migration removed. Left nullable deliberately.
