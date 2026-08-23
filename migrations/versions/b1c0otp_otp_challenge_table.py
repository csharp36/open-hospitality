"""Track B/B1 (D-B6): the otp_challenge table. NOT OrgScoped — no org_id, no
org_wall RLS policy: OTP gates signup, before any tenant exists.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1c0otp"
down_revision = "b1b0invite"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "otp_challenge",
        sa.Column("otp_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("target", sa.String(length=200), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Index("ix_otp_challenge_purpose_target", "purpose", "target"),
    )


def downgrade() -> None:
    op.drop_table("otp_challenge")
