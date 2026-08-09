"""pay_run_line department snapshot

Revision ID: b7c1d4a90e21
Revises: 664579948876
Create Date: 2026-07-17 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c1d4a90e21'
down_revision: Union[str, Sequence[str], None] = '664579948876'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pay_run_line", sa.Column("department_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_pay_run_line_department",
        "pay_run_line",
        "department",
        ["department_id"],
        ["department_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_pay_run_line_department", "pay_run_line", type_="foreignkey")
    op.drop_column("pay_run_line", "department_id")
