"""D-B7 (Track B/B1): the least-privilege usali_provisioner role's grants and a
role-specific permissive RLS policy on organization + role_assignment.

  - GRANT INSERT, SELECT on ONLY organization + role_assignment (+ USAGE on
    their identity sequences). NOTHING else.
  - A PERMISSIVE policy `provisioner_wall` TO usali_provisioner on those two
    tables, USING(true) WITH CHECK(true). Postgres OR-combines permissive
    policies, and a policy restricted TO a role does not apply to usali_app.

CREATE ROLE is cluster-level — this migration REFUSES loudly when the role is
missing, exactly like l2a0rlswall does for usali_app.
"""

import sqlalchemy as sa
from alembic import op

revision = "b1a0provrole"
down_revision = "m2a0perffoundations"
branch_labels = None
depends_on = None

PROVISIONER_ROLE = "usali_provisioner"
_POLICY = "provisioner_wall"
_TABLES = ("organization", "role_assignment")


def upgrade() -> None:
    conn = op.get_bind()
    if conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": PROVISIONER_ROLE},
    ).scalar() is None:
        raise RuntimeError(
            f"the provisioner database role {PROVISIONER_ROLE!r} does not exist "
            "— this migration's grants have nothing to land on. CREATE ROLE is "
            "cluster-level and lives outside the migration chain: "
            "scripts/cloud/bootstrap.sh provisions it in the cloud, "
            "scripts/dev_pg_init.sql in dev, and tests/orgwall.ensure_provisioner_role "
            "in the test container. Create the role, then re-run alembic."
        )
    for table in _TABLES:
        op.execute(f"GRANT INSERT, SELECT ON {table} TO {PROVISIONER_ROLE}")
        seq = conn.execute(
            sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
            {"t": table, "c": "org_id" if table == "organization" else "assignment_id"},
        ).scalar()
        if seq is not None:
            op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {PROVISIONER_ROLE}")
        op.execute(
            f"CREATE POLICY {_POLICY} ON {table} AS PERMISSIVE FOR ALL "
            f"TO {PROVISIONER_ROLE} USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {table}")
        op.execute(f"REVOKE INSERT, SELECT ON {table} FROM {PROVISIONER_ROLE}")
        seq = conn.execute(
            sa.text("SELECT pg_get_serial_sequence(:t, :c)"),
            {"t": table, "c": "org_id" if table == "organization" else "assignment_id"},
        ).scalar()
        if seq is not None:
            op.execute(f"REVOKE USAGE, SELECT ON SEQUENCE {seq} FROM {PROVISIONER_ROLE}")
