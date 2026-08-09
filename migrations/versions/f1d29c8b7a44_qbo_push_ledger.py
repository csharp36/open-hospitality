"""qbo push ledger

Revision ID: f1d29c8b7a44
Revises: 8ebf24a2f3ec
Create Date: 2026-07-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1d29c8b7a44'
down_revision: Union[str, Sequence[str], None] = '8ebf24a2f3ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('qbo_push_ledger',
    sa.Column('push_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('property_id', sa.String(length=50), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('qbo_je_id', sa.String(length=50), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('pushed_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('push_id'),
    sa.UniqueConstraint('property_id', 'business_date')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('qbo_push_ledger')
