# migrations/versions/m1a0propcfg_property_config_tables.py
"""Property config: room inventory, out-of-order rooms, fiscal calendar (#8).

Three OrgScoped tables the Analytics milestone divides by. Each joins the L2
database wall on the same terms as every other org-scoped table:
ENABLE/FORCE ROW LEVEL SECURITY plus the `org_wall` policy keyed on the
transaction-local app.org_id (the l2a0rlswall predicate, reused verbatim so the
policies cannot drift). The app role's DML grant arrives automatically through
the DEFAULT PRIVILEGES l2a0rlswall recorded for future tables.

Composite (org_id, property_id) FKs to `property` — the pay_schedule/department
wall: a single-column FK is validated with the referenced table's owner
privileges, past RLS, so an org-2 session could anchor a row to an org-1
property. The composite makes that cross-org reference unrepresentable.

No backfill — these are new config facts; inventing history would fabricate
room counts nobody stated. Downgrade drops the policies and the tables.
"""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import RLS_ORG_VAR

revision = "m1a0propcfg"
down_revision = "l9a0deptfk"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
_TABLES = ("room_inventory", "out_of_order_room", "fiscal_calendar")


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {table} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def upgrade() -> None:
    op.create_table(
        "room_inventory",
        sa.Column("inventory_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_room_inventory_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("total_rooms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("property_id", "effective_date", name="uq_room_inventory_prop_date"),
        sa.CheckConstraint("total_rooms > 0", name="ck_room_inventory_total_positive"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_room_inventory_property_org",
        ),
    )
    op.create_table(
        "out_of_order_room",
        sa.Column("ooo_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_out_of_order_room_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("room_count", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("end_date >= start_date", name="ck_ooo_range"),
        sa.CheckConstraint("room_count > 0", name="ck_ooo_count_positive"),
        sa.CheckConstraint(
            "reason_code IN ('maintenance', 'renovation', 'damage', 'deep_clean', 'other')",
            name="ck_ooo_reason_code",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_ooo_property_org",
        ),
    )
    op.create_table(
        "fiscal_calendar",
        sa.Column("property_id", sa.String(length=50), primary_key=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id", name="fk_fiscal_calendar_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("calendar_type", sa.String(length=20), nullable=False),
        sa.Column("fiscal_year_start_month", sa.Integer(), nullable=False),
        sa.Column("week_start_weekday", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("calendar_type IN ('calendar_month', '445')", name="ck_fiscal_type"),
        sa.CheckConstraint("fiscal_year_start_month BETWEEN 1 AND 12", name="ck_fiscal_start_month"),
        sa.CheckConstraint(
            "week_start_weekday IS NULL OR week_start_weekday BETWEEN 0 AND 6",
            name="ck_fiscal_weekday_range",
        ),
        sa.CheckConstraint(
            "(calendar_type = '445') = (week_start_weekday IS NOT NULL)",
            name="ck_fiscal_weekday_pair",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_fiscal_calendar_property_org",
        ),
    )
    for table in _TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY {_POLICY} ON {table}")
    op.drop_table("fiscal_calendar")
    op.drop_table("out_of_order_room")
    op.drop_table("room_inventory")
