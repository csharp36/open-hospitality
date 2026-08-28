# migrations/versions/n1a0nightaudit_night_audit_state.py
"""Night-audit state: the property's explicit current business date.

One `OrgScoped` row per property carrying `current_business_date` — the day the
hotel is operating in, advanced by the night-audit ROLL action once the night's
required reports have landed and the ledger checks pass. Until now the "current
date" was derived from the data (max fact date, attendance cutoff); the night
audit flow makes it explicit state so the roll can be gated and audited.

Joins the L2 wall on the same terms as every org-scoped table (ENABLE/FORCE ROW
LEVEL SECURITY + the verbatim l2a0rlswall predicate), with the composite
(org_id, property_id) FK that makes a cross-org property reference
unrepresentable (the pay_schedule / m1a0propcfg precedent).

No backfill: state rows are created lazily on first read, initialized from the
property's own data (max fact date + 1) — inventing a date here would fabricate
an operating day nobody stated. Downgrade drops the policy and the table.
"""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import RLS_ORG_VAR

revision = "n1a0nightaudit"
down_revision = "b1d0pmsinterest"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
_TABLE = "night_audit_state"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("property_id", sa.String(length=50), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_night_audit_state_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("current_business_date", sa.Date(), nullable=False),
        sa.Column("last_rolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_night_audit_state_property_org",
        ),
    )
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {_TABLE} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY {_POLICY} ON {_TABLE}")
    op.drop_table(_TABLE)
