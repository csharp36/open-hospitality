"""timecards

Revision ID: 4cc05c6c3763
Revises: 03f412becf70
Create Date: 2026-07-14 17:52:13.458940

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4cc05c6c3763'
down_revision: Union[str, Sequence[str], None] = '03f412becf70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "timecard",
        sa.Column("timecard_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("approved_by", sa.String(length=64), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("photos_purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.employee_id"]),
        sa.PrimaryKeyConstraint("timecard_id"),
        sa.UniqueConstraint("employee_id", "period_start"),
    )
    op.create_table(
        "timecard_adjustment",
        sa.Column("adjustment_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("timecard_id", sa.Integer(), nullable=False),
        sa.Column("punch_type", sa.String(length=20), nullable=False),
        sa.Column("adjusted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("actor_subject", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["timecard_id"], ["timecard.timecard_id"]),
        sa.PrimaryKeyConstraint("adjustment_id"),
    )
    op.add_column("punch", sa.Column("timecard_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_punch_timecard", "punch", "timecard", ["timecard_id"], ["timecard_id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_punch_timecard", "punch", type_="foreignkey")
    op.drop_column("punch", "timecard_id")
    op.drop_table("timecard_adjustment")
    op.drop_table("timecard")
