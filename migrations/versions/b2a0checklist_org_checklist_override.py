"""B4: the onboarding open-items checklist — `org_checklist_override`.

The checklist itself is DERIVED (usali.checklist probes what is actually
configured), so this table stores only the one fact nothing can derive: that
a tenant dismissed an optional item. Presence of a row means dismissed.

Joins the L2 database wall on the same terms as every other org-scoped table:
ENABLE/FORCE ROW LEVEL SECURITY plus the `org_wall` policy keyed on the
transaction-local `app.org_id` (the l2a0rlswall predicate, reused verbatim so
the two cannot drift). The app role's DML grant arrives automatically through
the DEFAULT PRIVILEGES l2a0rlswall recorded — no grant boilerplate here.

The `item_key` CHECK is the schema mirror of usali.checklist.ITEMS, literal on
purpose so the DB refuses an unknown key independently of the app import.

Downgrade drops the policy and the table: a dismissal is operator input a
re-seed does not need to reconstruct.
"""

from alembic import op
import sqlalchemy as sa

from usali.tenancy import RLS_ORG_VAR

revision = "b2a0checklist"
down_revision = "b1d0pmsinterest"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "org_checklist_override",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_org_checklist_override_org"),
            primary_key=True,
        ),
        sa.Column("item_key", sa.String(length=40), primary_key=True),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "item_key IN ('first_report', 'room_inventory', 'fiscal_calendar', "
            "'payroll', 'accounting', 'demand_feed', 'team')",
            name="ck_org_checklist_override_item_key",
        ),
    )
    op.execute("ALTER TABLE org_checklist_override ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_checklist_override FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON org_checklist_override "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY {_POLICY} ON org_checklist_override")
    op.drop_table("org_checklist_override")
