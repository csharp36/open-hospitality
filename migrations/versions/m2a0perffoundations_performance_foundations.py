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
    # NOTE: Tasks A2 + A3 will append property_stat_config + ingestion_coverage
    # create_table calls here, then a `for table in _NEW_TABLES: _enable_rls(table)`.


def downgrade() -> None:
    # NOTE: Tasks A2 + A3 will prepend DROP POLICY + drop_table for the two
    # new tables here.
    op.drop_constraint("ck_ooo_reason_code", "out_of_order_room", type_="check")
    op.create_check_constraint(
        "ck_ooo_reason_code", "out_of_order_room", f"reason_code IN {_OOO_REASONS_OLD}"
    )
