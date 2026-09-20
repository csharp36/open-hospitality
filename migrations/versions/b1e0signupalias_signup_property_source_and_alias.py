# migrations/versions/b1e0signupalias_signup_property_source_and_alias.py
"""Signup-created properties: uppercase pms_source and a detection alias.

Until property_registry.create_first_property was fixed (OH-22 Task 8), a
property created through /api/signup/complete stored pms_source verbatim from
the API's lowercase Literal ("opera", "hotelkey", ...) and got NO
property_detection_alias row. detect() resolves a report's property from
alias rows only and compares the alias's pms_source with the report
signature's uppercase spelling as plain strings, so such a property could
never be detected by any upload path, and the night audit's fact reads keyed
on the row's own pms_source would have missed the uppercase-stamped facts.

Two data statements, idempotent by construction:

1. UPPER() every property.pms_source that differs from its uppercase form.
2. For every property with no alias row at all, insert one whose match phrase
   is the property's name (the text typed at signup -- the only text its
   exports can be matched on), under the property's org and (now uppercase)
   source. Properties with a NULL or blank name are left alone: an empty
   match phrase would match every header, and the migration must not fail on
   a row it cannot sensibly alias.

Wrapped in NO FORCE / FORCE ROW LEVEL SECURITY on both tables (2026-09-20):
`property` and `property_detection_alias` are ENABLE + FORCE RLS, and the
cloud migrate job runs `alembic upgrade head` as the non-superuser table
OWNER with no org bound (scripts/cloud/job.sh:33 runs the upgrade;
scripts/cloud/env.sh:13-16 leaves USALI_DB_USER unset so the job keeps the
owner identity; scripts/cloud/bootstrap.sh:174-175 records that Cloud SQL
users are never SUPERUSER and get no BYPASSRLS). Under FORCE that identity
sees zero rows, so the bare UPDATE and INSERT ... SELECT would run clean and
change nothing. Lifting FORCE lets the owner bypass the policy for the two
statements; the policy itself stays ENABLEd for every other role throughout,
and migrations/env.py:29-30 runs the whole upgrade inside one
begin_transaction(), so the window closes with the backfill or not at all.
test_b1e_signup_alias_migration.py runs `run` as a non-superuser owner and
checks both tables are FORCE again afterwards.

Revision ID: b1e0signupalias
Revises: n2a0nightadjust
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision = "b1e0signupalias"
down_revision = "n2a0nightadjust"
branch_labels = None
depends_on = None


def backfill(conn: Connection) -> None:
    """The two data statements, separable from `upgrade` so a test can run
    them a second time against a migrated database and prove they converge
    (test_b1e_signup_alias_migration.py)."""
    conn.execute(sa.text(
        "UPDATE property SET pms_source = UPPER(pms_source) "
        "WHERE pms_source <> UPPER(pms_source)"
    ))
    conn.execute(sa.text(
        "INSERT INTO property_detection_alias "
        "(org_id, property_id, pms_source, match_phrase) "
        "SELECT p.org_id, p.property_id, p.pms_source, p.name FROM property p "
        "WHERE p.name IS NOT NULL AND btrim(p.name) <> '' "
        "AND NOT EXISTS (SELECT 1 FROM property_detection_alias a "
        "WHERE a.property_id = p.property_id)"
    ))


_FORCED_TABLES = ("property", "property_detection_alias")


def run(conn: Connection) -> None:
    """The upgrade body against a plain connection (see the docstring for
    why FORCE is lifted): a test can run it as a non-superuser owner."""
    for table in _FORCED_TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
    backfill(conn)
    for table in _FORCED_TABLES:
        conn.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def upgrade() -> None:
    run(op.get_bind())


def downgrade() -> None:
    # Deliberately a no-op. Uppercase is the spelling every other writer of
    # pms_source already used (properties.yaml, the adapters), so restoring
    # the lowercase rows would only re-break detection for those properties;
    # and an alias row is what seed-properties creates for every registered
    # property, so leaving it in place is the pre-signup shape, not a
    # leftover. Neither the old schema nor any older revision's downgrade
    # depends on either being reverted.
    pass
