# migrations/versions/o1a0intake_email_intake_tables.py
"""OH-23: the emailed night-audit intake tables (D-OH23.3, D-OH23.7).

Two tables, two different tenancy shapes.

`property_intake_address` is NOT OrgScoped and gets NO org_wall policy, on
the `invite` / `otp_challenge` precedent (b1b0invite, b1c0otp): the webhook
resolves a message's local part to a row on the UNBOUND base session,
before any org is known, and only then opens an org-bound session on the
row's `org_id`. A policy keyed on `app.org_id` would refuse that lookup.

It differs from `invite` in one way worth naming: it DOES carry a NOT NULL
`org_id`, because the address is what resolves the tenant. So it is the one
table in the schema with an org key and no RLS wall, and the wall it does
have is the composite `(org_id, property_id)` FK — which makes a row naming
another org's property unrepresentable — plus explicit `org_id` filtering in
the operator routes. `test_l1_every_tenant_table_carries_org_id_and_the_backfill_landed_org_1`
is where the NOT NULL org_id half is checked; it treats this table as
tenant-owned, which is why the table is NOT in that test's
`_L1_ORG_INDEPENDENT` set (that set means "no org_id column at all").

`email_intake_event` is OrgScoped and joins the L2 database wall on the
same terms as every other org-scoped table: ENABLE + FORCE ROW LEVEL
SECURITY and the `org_wall` policy built from `usali.tenancy.RLS_ORG_VAR`
(the l5a0orgsettings / n1a0nightaudit template, so the predicate cannot
drift from l2a0rlswall's). No GRANT here: usali_app's DML arrives through
l2a0rlswall's ALTER DEFAULT PRIVILEGES for future tables.

`outcome` carries a CHECK over the closed set D-OH23.7 names — the
refuse-unknown posture at the schema, so no code path can land an outcome
the event log's readers do not know how to render.

Downgrade drops both tables; the policy goes with its table.
"""

import sqlalchemy as sa
from alembic import op

from usali.tenancy import RLS_ORG_VAR

revision = "o1a0intake"
down_revision = "b1e0signupalias"
branch_labels = None
depends_on = None

_POLICY = "org_wall"
_PREDICATE = f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
_ADDRESS = "property_intake_address"
_EVENT = "email_intake_event"

_OUTCOMES = (
    "('ingested', 'partial', 'duplicate', 'no_attachment', 'sender_rejected', "
    "'wrong_property', 'not_a_night_audit_report', 'unreadable', 'failed', "
    "'revoked_address')"
)


def upgrade() -> None:
    op.create_table(
        _ADDRESS,
        sa.Column("address_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("local_part", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id",
                                name="fk_property_intake_address_org"),
                  nullable=False),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sender_domains", sa.JSON(), nullable=True),
        sa.UniqueConstraint("local_part",
                            name="uq_property_intake_address_local_part"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_property_intake_address_property_org"),
    )
    op.create_table(
        _EVENT,
        sa.Column("event_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(),
                  sa.ForeignKey("organization.org_id",
                                name="fk_email_intake_event_org"),
                  server_default=sa.text("1"), nullable=False, index=True),
        sa.Column("address_id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.String(length=50), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("envelope_from", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=True),
        sa.Column("auth_result", sa.String(length=1000), nullable=True),
        sa.Column("message_id", sa.String(length=320), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("attachments", sa.JSON(),
                  server_default=sa.text("'[]'::json"), nullable=False),
        sa.CheckConstraint(f"outcome IN {_OUTCOMES}",
                           name="ck_email_intake_event_outcome"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_email_intake_event_property_org"),
    )
    op.execute(f"ALTER TABLE {_EVENT} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_EVENT} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {_EVENT} "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY {_POLICY} ON {_EVENT}")
    op.drop_table(_EVENT)
    op.drop_table(_ADDRESS)
