"""L5: per-org integration config — the `org_settings` table (decision 5).

The stores outside Postgres go per-org. This is the Postgres half of that:
a per-tenant `crm_provider`, so the demand feed a hotel group pulls from is
a property of the ORG (read under the active org's RLS), not a process-wide
env switch shared by every tenant. `USALI_CRM_PROVIDER` becomes the SEED
default for org 1 only (`ensure_default_org`); at runtime the value is read
from this table.

`org_id` is BOTH the primary key and the FK to `organization` — one row per
org, org-scoped by its own key exactly like `organization` itself. The table
therefore joins the L2 database wall on the SAME terms as every other
org-scoped table: `ENABLE`/`FORCE ROW LEVEL SECURITY` plus the `org_wall`
policy keyed on the transaction-local `app.org_id` (the l2a0rlswall
predicate, reused verbatim so the two cannot drift). The app role's DML
grant arrives automatically through the DEFAULT PRIVILEGES l2a0rlswall
recorded for future tables — no grant boilerplate here.

`crm_provider` carries a CHECK for the closed set ('' | delphi | tripleseat)
— the refuse-unknown posture (J4) enforced at the schema, so no seed or
provisioning path can land a provider name the feed factory would reject at
runtime. EMPTY is the OFF state.

Downgrade drops the policy and the table (the org_settings rows are pure
config a re-seed reconstructs from env/operator input — not the I6
carry-rows-through case).
"""

from alembic import op
import sqlalchemy as sa

from usali.tenancy import RLS_ORG_VAR

revision = "l5a0orgsettings"
down_revision = "l4a0orggrant"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "org_settings",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey("organization.org_id", name="fk_org_settings_org"),
            primary_key=True,
        ),
        sa.Column(
            "crm_provider",
            sa.String(length=20),
            server_default="",
            nullable=False,
        ),
        sa.CheckConstraint(
            "crm_provider IN ('', 'delphi', 'tripleseat')",
            name="ck_org_settings_crm_provider",
        ),
    )
    op.execute("ALTER TABLE org_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON org_settings "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY {_POLICY} ON org_settings")
    op.drop_table("org_settings")
