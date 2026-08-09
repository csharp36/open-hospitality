"""e1: drop the wage_jurisdiction server default

e1b0 added `wage_jurisdiction NOT NULL DEFAULT 'US-CA'` so existing rows
backfilled correctly. Leaving the default on the table permanently means a NEW
property -- say in Texas -- silently inherits California overtime law: daily
over-8 at 1.5x and over-12 double time, applied to a state that has neither.

That is precisely the failure overtime_rules.rules_for() exists to prevent. Its
refusal fires on an UNRECOGNIZED jurisdiction, never on an UNSET one, so a
lingering default converts a loud refusal into a silent miscosting.

The column stays NOT NULL: creating a property must be an explicit decision
about which wage rules price its hours.

Revision ID: e1f0nodefault
Revises: e1e0oneprimary
"""

from alembic import op

revision = "e1f0nodefault"
down_revision = "e1e0oneprimary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE property ALTER COLUMN wage_jurisdiction DROP DEFAULT")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE property ALTER COLUMN wage_jurisdiction SET DEFAULT 'US-CA'"
    )
