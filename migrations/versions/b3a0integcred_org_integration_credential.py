"""OH-17: per-tenant integration credentials, and the retirement of org_settings.

The row IS the connection (D-OH17.1): provider AND credentials together, so a
tenant cannot pick a provider without supplying credentials for it. That
absorbs `org_settings.crm_provider` — its only column — so `org_settings` is
dropped here rather than left standing empty.

The org_settings rows are NOT carried forward (D-OH17.14). The matching secret
lives in env, and a data migration that reads env is fragile. Safe by
enumeration, not assumption: the only writer was `ensure_default_org` for org
1, and no SPA page ever wrote crm_provider — so the only row that can exist is
org 1's, which the seed reconstructs from the same env. This is the posture
l5a0orgsettings' own downgrade already recorded: "pure config a re-seed
reconstructs from env/operator input — not the I6 carry-rows-through case."

Joins the L2 database wall on the same terms as every other org-scoped table:
ENABLE/FORCE ROW LEVEL SECURITY plus the `org_wall` policy keyed on the
transaction-local `app.org_id`, predicate reused verbatim from l2a0rlswall so
the two cannot drift. No GRANT — the DEFAULT PRIVILEGES l2a0rlswall recorded
cover future tables.

The CHECK is the schema mirror of `usali.integrations.PROVIDERS` (arrives with
the connect surfaces; it does not exist yet at this revision), kept literal so
the DB refuses a malformed row independently of the app import.
"""

from alembic import op
import sqlalchemy as sa

from usali.tenancy import RLS_ORG_VAR

revision = "b3a0integcred"
down_revision = "b2a0checklist"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"

# BYTE-IDENTICAL to `models.OrgIntegrationCredential.__table_args__`, and
# duplicated rather than imported ON PURPOSE. A migration pins the schema as of
# THIS revision; importing the model would let a later edit silently rewrite
# what an old revision claims to have created, and `alembic upgrade` would then
# build a different table than the one this file documents. The model's
# docstring points here and this comment points back: EDIT BOTH OR NEITHER —
# `tests/test_l1_org_wall_migration.py`'s `compare_metadata` parity check is
# what fails if you edit one.
_CHECK = (
    "(integration = 'payroll' AND provider = 'gusto'"
    "  AND api_token IS NOT NULL AND company_id IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND subscription_key IS NULL AND api_key IS NULL)"
    " OR (integration = 'payroll' AND provider = 'adp'"
    "  AND client_id IS NOT NULL AND client_secret IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND subscription_key IS NULL AND api_key IS NULL)"
    " OR (integration = 'accounting' AND provider = 'qbo'"
    "  AND refresh_token IS NOT NULL AND realm_id IS NOT NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND subscription_key IS NULL AND api_key IS NULL)"
    " OR (integration = 'demand_feed' AND provider = 'delphi'"
    "  AND subscription_key IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND api_key IS NULL)"
    " OR (integration = 'demand_feed' AND provider = 'tripleseat'"
    "  AND api_key IS NOT NULL"
    "  AND refresh_token IS NULL AND realm_id IS NULL"
    "  AND api_token IS NULL AND company_id IS NULL"
    "  AND client_id IS NULL AND client_secret IS NULL"
    "  AND subscription_key IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "org_integration_credential",
        sa.Column(
            "org_id",
            sa.Integer(),
            sa.ForeignKey(
                "organization.org_id", name="fk_org_integration_credential_org"
            ),
            primary_key=True,
        ),
        sa.Column("integration", sa.String(length=20), primary_key=True),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("realm_id", sa.String(length=64), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("api_token", sa.Text(), nullable=True),
        sa.Column("company_id", sa.String(length=64), nullable=True),
        sa.Column("client_id", sa.String(length=128), nullable=True),
        sa.Column("client_secret", sa.Text(), nullable=True),
        sa.Column("subscription_key", sa.Text(), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # String(64), matching every other actor column in models.py
        # (created_by, enrolled_by, approved_by, ...). A Keycloak subject is a
        # UUID, so 64 is ample.
        sa.Column("connected_by", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            _CHECK, name="ck_org_integration_credential_provider_fields"
        ),
    )
    op.execute("ALTER TABLE org_integration_credential ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE org_integration_credential FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON org_integration_credential "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )

    # D-OH17.1 / D-OH17.14: crm_provider is absorbed, rows are not carried.
    op.execute(f"DROP POLICY {_POLICY} ON org_settings")
    op.drop_table("org_settings")


def downgrade() -> None:
    # THIS DOWNGRADE DESTROYS EVERY TENANT'S CONNECTED INTEGRATIONS, and the
    # loss is not symmetric with the upgrade's. The docstring above says
    # dropping `org_settings` was safe because `crm_provider` was pure config a
    # re-seed reconstructs from env; that precedent does NOT transfer to this
    # table. `org_integration_credential` holds each tenant's own payroll,
    # accounting and demand-feed credentials, and the QBO `refresh_token` in
    # particular is a ROTATING value whose current lineage exists nowhere else:
    # Intuit has already consumed every earlier token, and env holds only the
    # bootstrap one for org 1. An upgrade -> downgrade -> upgrade cycle
    # therefore leaves every tenant disconnected, and QBO cannot be re-seeded
    # at all — each tenant must re-run Intuit consent through
    # `/api/integrations/accounting/authorize`.
    #
    # Dump the table before running this against anything holding real
    # connections. Nothing here reconstructs it.
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
    op.execute(f"DROP POLICY {_POLICY} ON org_integration_credential")
    op.drop_table("org_integration_credential")
