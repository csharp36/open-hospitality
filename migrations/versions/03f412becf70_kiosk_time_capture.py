"""kiosk time capture

Revision ID: 03f412becf70
Revises: ece60731aa05
Create Date: 2026-07-14 16:26:53.285800

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '03f412becf70'
down_revision: Union[str, Sequence[str], None] = 'ece60731aa05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "property",
        sa.Column(
            "timezone",
            sa.String(length=50),
            server_default="America/Los_Angeles",
            nullable=False,
        ),
    )
    op.create_table(
        "kiosk_device",
        sa.Column("device_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("enrolled_by", sa.String(length=64), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["property_id"], ["property.property_id"]),
        sa.PrimaryKeyConstraint("device_id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "punch",
        sa.Column("punch_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("kiosk_device_id", sa.Integer(), nullable=False),
        sa.Column("punch_type", sa.String(length=20), nullable=False),
        sa.Column("punched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("photo_key", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.employee_id"]),
        sa.ForeignKeyConstraint(["kiosk_device_id"], ["kiosk_device.device_id"]),
        sa.PrimaryKeyConstraint("punch_id"),
    )


def downgrade() -> None:
    op.drop_table("punch")
    op.drop_table("kiosk_device")
    op.drop_column("property", "timezone")
