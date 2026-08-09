"""employee pay_rate

Revision ID: 712ad594aeb3
Revises: 4cc05c6c3763
Create Date: 2026-07-14 21:39:19.943839

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '712ad594aeb3'
down_revision: Union[str, Sequence[str], None] = '4cc05c6c3763'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employee", sa.Column("pay_rate", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("employee", "pay_rate")
