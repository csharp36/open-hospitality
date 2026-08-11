"""Performance foundations: DNR reason codes, per-property stat config, and the
ingestion-coverage table (#9)."""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import RLS_ORG_VAR

revision = "m2a0perffoundations"
down_revision = "m1a0propcfg"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
_NEW_TABLES = ("property_stat_config", "ingestion_coverage")

_OOO_REASONS_OLD = "('maintenance', 'renovation', 'damage', 'deep_clean', 'other')"
_OOO_REASONS_NEW = ("('maintenance', 'renovation', 'damage', 'deep_clean', 'other', "
                    "'do_not_rent', 'owner_occupied')")


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def upgrade() -> None:
    # Widen the out-of-service reason vocabulary to carry DNR (do_not_rent) and
    # owner_occupied — held-out rooms reduce rooms_available exactly like OOO.
    op.drop_constraint("ck_ooo_reason_code", "out_of_order_room", type_="check")
    op.create_check_constraint(
        "ck_ooo_reason_code", "out_of_order_room", f"reason_code IN {_OOO_REASONS_NEW}"
    )
    op.create_table(
        "property_stat_config",
        sa.Column("property_id", sa.String(length=50), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_property_stat_config_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("adr_room_basis", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("adr_room_basis IN ('as_reported', 'exclude_comp_house')",
                           name="ck_stat_config_adr_basis"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_property_stat_config_property_org"),
    )
    _enable_rls("property_stat_config")
    # NOTE: Task A3 will append the ingestion_coverage create_table here and
    # switch this to a `for table in _NEW_TABLES: _enable_rls(table)` loop.


def downgrade() -> None:
    # NOTE: Task A3 will add ingestion_coverage's DROP POLICY + drop_table here
    # and switch to a loop over _NEW_TABLES.
    op.execute(f"DROP POLICY {_POLICY} ON property_stat_config")
    op.drop_table("property_stat_config")
    op.drop_constraint("ck_ooo_reason_code", "out_of_order_room", type_="check")
    op.create_check_constraint(
        "ck_ooo_reason_code", "out_of_order_room", f"reason_code IN {_OOO_REASONS_OLD}"
    )
