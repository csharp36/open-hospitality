# migrations/versions/o1a0intake_email_intake_tables.py
"""OH-23: the emailed night-audit intake tables (D-OH23.3, D-OH23.7).

Two tables, two different tenancy shapes.

`property_intake_address` is NOT OrgScoped and gets NO org_wall policy, on
the `invite` / `otp_challenge` precedent (b1b0invite, b1c0otp). D-OH23.3
requires the webhook resolve a message's local part before any org is
known — a lookup on the unbound base session — and only then bind a
session to the row's `org_id`; a policy keyed on `app.org_id` would refuse
that lookup. The no-policy choice is named, not inferred: the exclusion
loop in `test_l2_rls_wall.py::test_the_rls_inventory_is_complete_and_forced`
asserts this table is absent from the policied set.

It differs from `invite` in one way worth naming: it DOES carry a NOT NULL
`org_id`, because the address is what resolves the tenant.
`test_l1_every_tenant_table_carries_org_id_and_the_backfill_landed_org_1`
checks that half (it treats this table as tenant-owned, which is why the
table is NOT in that test's `_L1_ORG_INDEPENDENT` set — that set means "no
org_id column at all"). What stands in for the missing wall is the
composite `(org_id, property_id)` FK, which makes a row naming another
org's property unrepresentable, plus per-org filtering in the operator
routes, which D-OH23.3 requires and
`tests/test_intake_address_api.py::test_an_address_of_another_org_is_invisible_and_unrotatable`
checks.

Two constraints beyond the FK. `uq_property_intake_address_active` is a
partial unique on `(org_id, property_id) WHERE revoked_at IS NULL`: one
live address per property is enforced here, so two concurrent creates
cannot both land and leave a rotate revoking only one.
`ck_property_intake_address_local_part_lower` refuses a local part that is
not already lowercase, so the unique on `local_part` is case-insensitive
in effect — the same refuse-unknown posture `outcome` takes below.

`email_intake_event` is OrgScoped and joins the L2 database wall on the
same terms as every other org-scoped table: ENABLE + FORCE ROW LEVEL
SECURITY and the `org_wall` policy. `_PREDICATE` below is a literal copy
of l2a0rlswall's, sharing only the imported `usali.tenancy.RLS_ORG_VAR`
with it; `test_l2_rls_wall.py::test_the_stacked_migrations_share_the_l2_rls_predicate`
is the drift guard, pinning this copy byte-equal to l2's. No GRANT here:
usali_app's DML arrives through l2a0rlswall's ALTER DEFAULT PRIVILEGES for
future tables.

`outcome` carries a CHECK over the closed set D-OH23.7 names — the
refuse-unknown posture at the schema, so no code path can land an outcome
the event log's readers do not know how to render. `_OUTCOMES` is
duplicated in `EmailIntakeEvent.__table_args__`; compare_metadata does not
compare CHECKs, so Task 3's INTAKE_OUTCOMES pin is what will hold the two
copies together.

Both JSON columns are JSONB: `json` has no equality operator and cannot be
indexed, and the tables are empty at this revision so there is nothing to
cast.

downgrade() drops the policy explicitly, then both tables; the partial
unique index and both CHECKs go with their tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

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
        sa.Column("sender_domains", JSONB(), nullable=True),
        sa.UniqueConstraint("local_part",
                            name="uq_property_intake_address_local_part"),
        sa.CheckConstraint("local_part = lower(local_part)",
                           name="ck_property_intake_address_local_part_lower"),
        sa.ForeignKeyConstraint(
            ["org_id", "property_id"], ["property.org_id", "property.property_id"],
            name="fk_property_intake_address_property_org"),
    )
    op.create_index(
        "uq_property_intake_address_active", _ADDRESS, ["org_id", "property_id"],
        unique=True, postgresql_where=sa.text("revoked_at IS NULL"),
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
        sa.Column("attachments", JSONB(),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
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
