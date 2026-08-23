"""Track B/B1 (D-B3): the invite-gate table. NOT OrgScoped — no org_id, no
org_wall RLS policy: an invite precedes any tenant. usali_app is granted DML on
it by l2a0rlswall's ALTER DEFAULT PRIVILEGES (future tables), so no grant here.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1b0invite"
down_revision = "b1a0provrole"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invite",
        sa.Column("invite_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("consumed_org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_invite_consumed_org"),
                  nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_invite_token_hash"),
        sa.CheckConstraint("status IN ('pending', 'consumed', 'revoked')",
                           name="ck_invite_status"),
    )


def downgrade() -> None:
    op.drop_table("invite")
