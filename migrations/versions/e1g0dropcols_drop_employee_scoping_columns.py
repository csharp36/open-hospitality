"""e1 Task 9: drop Employee.property_id / department_id / position_id

The end of the migration. Property, department and position now live on
employee_assignment, effective-dated, one row per placement — because a person
can work at two hotels doing two different jobs, which three single FKs on the
employee row could not express for 21 of the pilot's 28 staff.

WHY THIS IS THE POINT OF THE TASK, not cleanup. While these columns existed,
every consumer needed a fallback for rows the assignment table had nothing to
say about. Those fallbacks — the migration ramps — were the single most
error-prone thing in E1: one shipped as a kiosk readmitting terminated
employees, and an adversarial review then found the same mistake in five more,
including an authorization bypass and a path that let a stale column declare
someone FLSA-exempt and zero their labor cost.

Dropping the columns deletes the class of bug, not an instance of it. There is
nothing left to fall back TO.

IRREVERSIBLE IN PRACTICE. downgrade() recreates the columns but cannot recover
which of several assignments was "the" property, and any employee holding two
would have to lose one. It backfills from the primary assignment as the best
available reconstruction and is a disaster-recovery path, not a routine one.

Revision ID: e1g0dropcols
Revises: e1f0nodefault
"""

import sqlalchemy as sa
from alembic import op

revision = "e1g0dropcols"
down_revision = "e1f0nodefault"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("employee", "position_id")
    op.drop_column("employee", "department_id")
    op.drop_column("employee", "property_id")


def downgrade() -> None:
    op.add_column("employee", sa.Column("property_id", sa.String(length=50), nullable=True))
    op.add_column("employee", sa.Column("department_id", sa.Integer(), nullable=True))
    op.add_column("employee", sa.Column("position_id", sa.Integer(), nullable=True))
    # Best-available reconstruction: the primary assignment. Lossy by nature —
    # a second assignment has nowhere to go.
    op.execute(
        """
        UPDATE employee e SET
            property_id = a.property_id,
            department_id = a.department_id,
            position_id = a.position_id
        FROM employee_assignment a
        WHERE a.employee_id = e.employee_id AND a.is_primary
        """
    )
    op.execute(
        "UPDATE employee SET property_id = 'UNKNOWN' WHERE property_id IS NULL"
    )
    op.alter_column("employee", "property_id", nullable=False)
