"""ledger balance stage and fact tables

Revision ID: 8ebf24a2f3ec
Revises: ca7362bc3650
Create Date: 2026-07-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8ebf24a2f3ec'
down_revision: Union[str, Sequence[str], None] = 'ca7362bc3650'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pms_ledger_balance_stage',
    sa.Column('ledger_stage_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('property_id', sa.String(length=50), nullable=False),
    sa.Column('pms_source', sa.String(length=20), nullable=False),
    sa.Column('report_type', sa.String(length=40), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('ledger_label', sa.String(length=200), nullable=False),
    sa.Column('amount', sa.Numeric(precision=15, scale=4), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('source_file', sa.String(length=255), nullable=False),
    sa.Column('ingest_batch_id', sa.BigInteger(), nullable=False),
    sa.Column('row_hash', sa.String(length=64), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ingest_batch_id'], ['ingest_batch.batch_id'], ),
    sa.PrimaryKeyConstraint('ledger_stage_id'),
    sa.UniqueConstraint('pms_source', 'business_date', 'row_hash')
    )
    op.create_table('usali_ledger_balance_fact',
    sa.Column('ledger_fact_id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('property_id', sa.String(length=50), nullable=False),
    sa.Column('pms_source', sa.String(length=20), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('ledger_code', sa.String(length=50), nullable=False),
    sa.Column('ledger_name', sa.String(length=100), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('amount', sa.Numeric(precision=15, scale=4), nullable=False),
    sa.Column('ingest_batch_id', sa.BigInteger(), nullable=False),
    sa.Column('ledger_stage_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['ingest_batch_id'], ['ingest_batch.batch_id'], ),
    sa.ForeignKeyConstraint(['ledger_stage_id'], ['pms_ledger_balance_stage.ledger_stage_id'], ),
    sa.PrimaryKeyConstraint('ledger_fact_id'),
    sa.UniqueConstraint('ledger_stage_id'),
    sa.UniqueConstraint('property_id', 'pms_source', 'business_date', 'ledger_code')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('usali_ledger_balance_fact')
    op.drop_table('pms_ledger_balance_stage')
