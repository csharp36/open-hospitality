# migrations/versions/n2a0nightadjust_night_audit_adjustment.py
"""Night-audit balance corrections: the append-only record behind a direct edit.

The night-audit flow lets the auditor CORRECT a prior close directly (the
stored ledger-balance fact is updated in place) when the cross-night
roll-forward finds a hole the PMS export cannot fix. The stage row keeps what
the PMS originally said; this table records every correction — old value, new
value, mandatory reason, actor — so the direct edit stays attributable.

OrgScoped, RLS'd, composite (org_id, property_id) FK: the standard wall.
"""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import RLS_ORG_VAR

revision = "n2a0nightadjust"
down_revision = "n1a0nightaudit"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
_TABLE = "night_audit_adjustment"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("adjustment_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_night_audit_adjustment_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("ledger_code", sa.String(length=50), nullable=False),
        sa.Column("old_amount", sa.Numeric(15, 4), nullable=False),
        sa.Column("new_amount", sa.Numeric(15, 4), nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("actor_subject", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_night_audit_adjustment_property_org",
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
