"""e1: property.wage_jurisdiction

Which jurisdiction's wage rules price a property's hours. Deliberately separate
from `timezone`: America/Los_Angeles spans California, Nevada and Washington,
whose daily-overtime rules differ or are absent, so timezone is not a safe proxy.

Existing rows default to US-CA, which is correct for both pilot properties and
is the rule set they have been costed under to date. Anything else must be set
explicitly — usali.overtime_rules refuses jurisdictions it has no verified
ruleset for rather than falling back.

Revision ID: e1b0jurisdiction
Revises: e1a0assignments
"""

import sqlalchemy as sa
from alembic import op

revision = "e1b0jurisdiction"
down_revision = "e1a0assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "property",
        sa.Column(
            "wage_jurisdiction",
            sa.String(length=10),
            nullable=False,
            server_default="US-CA",
        ),
    )


def downgrade() -> None:
    op.drop_column("property", "wage_jurisdiction")
