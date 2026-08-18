"""Track B/B1 Part-2: the pms_interest_request table. NOT OrgScoped — no
org_wall RLS policy: platform-level demand data. usali_app gets DML via
l2a0rlswall's ALTER DEFAULT PRIVILEGES (future tables), so no grant here."""

import sqlalchemy as sa
from alembic import op

revision = "b1d0pmsinterest"
down_revision = "b1c0otp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pms_interest_request",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_alias", sa.String(length=63), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("raw_pms", sa.String(length=60), nullable=False),
        sa.Column("normalized_pms", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=12), server_default="new", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_alias", "normalized_pms",
                            name="uq_pms_interest_org_norm"),
    )


def downgrade() -> None:
    op.drop_table("pms_interest_request")
