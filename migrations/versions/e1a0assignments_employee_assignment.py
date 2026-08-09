"""e1: employee_assignment + lossless backfill

Creates the effective-dated assignment table and seeds it from the three
`Employee` scoping columns it will eventually replace. The backfill is
deliberately LOSSLESS and conservative: every existing employee gets exactly one
PRIMARY assignment mirroring their current property/department/position, with
`effective_from` at the payroll anchor so no already-costed period sees its
attribution move.

The old columns are NOT dropped here — that is a separate migration at the end
of E1, once every reader has been cut over.

Revision ID: e1a0assignments
Revises: 178480aede62
"""

import sqlalchemy as sa
from alembic import op

revision = "e1a0assignments"
down_revision = "178480aede62"
branch_labels = None
depends_on = None

# The payroll anchor (Settings.payroll_period_anchor). Hard-coded rather than
# imported: a migration must reproduce the same result years from now even if
# the setting changes.
_ANCHOR = "2026-01-05"


def upgrade() -> None:
    op.create_table(
        "employee_assignment",
        sa.Column("assignment_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.String(), nullable=False),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("position_id", sa.Integer(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.employee_id"]),
        sa.ForeignKeyConstraint(["property_id"], ["property.property_id"]),
        sa.ForeignKeyConstraint(["department_id"], ["department.department_id"]),
        sa.ForeignKeyConstraint(["position_id"], ["position.position_id"]),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("employee_id", "property_id", "effective_from"),
    )
    op.create_index(
        "ix_employee_assignment_employee", "employee_assignment", ["employee_id"]
    )
    op.create_index(
        "ix_employee_assignment_property", "employee_assignment", ["property_id"]
    )

    # A terminated employee's assignment closes the day AFTER their termination
    # date, so effective-dating alone answers "was this person employed here on
    # date X" -- and their LAST DAY WORKED stays inside the assignment.
    #
    # effective_to is EXCLUSIVE. Setting it to termination_date itself excluded
    # the final day: those hours fell out of the pay run that owed them and were
    # costed to a null department. This matches onboarding.terminate_employee,
    # which does the same +1 at runtime; the two must agree or a backfilled
    # termination and a live one behave differently.
    #
    # GREATEST guards the inverted interval where someone was terminated BEFORE
    # the anchor: effective_to must never precede effective_from.
    op.execute(
        f"""
        INSERT INTO employee_assignment
            (employee_id, property_id, department_id, position_id,
             is_primary, status, effective_from, effective_to)
        SELECT employee_id, property_id, department_id, position_id,
               true,
               CASE WHEN termination_date IS NULL THEN 'active' ELSE 'inactive' END,
               DATE '{_ANCHOR}',
               CASE WHEN termination_date IS NULL THEN NULL
                    ELSE GREATEST(termination_date + 1, DATE '{_ANCHOR}')
               END
        FROM employee
        """
    )


def downgrade() -> None:
    op.drop_index("ix_employee_assignment_property", table_name="employee_assignment")
    op.drop_index("ix_employee_assignment_employee", table_name="employee_assignment")
    op.drop_table("employee_assignment")
