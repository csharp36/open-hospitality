"""workforce property tables

Revision ID: 0da53c099d83
Revises: f1d29c8b7a44
Create Date: 2026-07-13 18:29:10.505794

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0da53c099d83'
down_revision: Union[str, Sequence[str], None] = 'f1d29c8b7a44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "organization",
        sa.Column("org_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("org_id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "property",
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("pms_source", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organization.org_id"]),
        sa.PrimaryKeyConstraint("property_id"),
    )
    op.create_table(
        "property_detection_alias",
        sa.Column("alias_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("pms_source", sa.String(length=20), nullable=False),
        sa.Column("match_phrase", sa.String(length=200), nullable=False),
        sa.ForeignKeyConstraint(["property_id"], ["property.property_id"]),
        sa.PrimaryKeyConstraint("alias_id"),
        sa.UniqueConstraint("property_id", "pms_source", "match_phrase"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("property_detection_alias")
    op.drop_table("property")
    op.drop_table("organization")
