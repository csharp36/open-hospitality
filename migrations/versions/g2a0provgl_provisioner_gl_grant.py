"""OH-27 G2: extend the least-privilege provisioner role onto gl_account
(D-OH27.1's provisioning wire-up).

b1a0provrole scoped `usali_provisioner` to INSERT/SELECT on ONLY
`organization` + `role_assignment`, with a matching PERMISSIVE
`provisioner_wall` policy on each ("NOTHING else"). `provision_tenant`
now also seeds the new org's USALI chart (`gl_chart.seed_chart`) in the
SAME transaction as the org + grant, so `usali_provisioner` needs the
same INSERT/SELECT + permissive-policy carve-out on `gl_account` that
the other two tables already have — otherwise the self-service signup
path's least-privilege session cannot see or write the table at all:
g1a0glcore's `org_wall` policy carries no `TO` clause, so it applies to
every role including this one, and a provisioner session never sets
`app.org_id` (it is un-instrumented by design), so the predicate is
fail-closed exactly as documented there.

Deliberately NOT UPDATE/DELETE — seeding is insert-only (gl_chart.py's
own contract), so the provisioner never gets write access it would need
to violate that.
"""

import sqlalchemy as sa
from alembic import op

revision = "g2a0provgl"
down_revision = "g1a0glcore"
branch_labels = None
depends_on = None

PROVISIONER_ROLE = "usali_provisioner"
_POLICY = "provisioner_wall"
_TABLE = "gl_account"


def upgrade() -> None:
    conn = op.get_bind()
    if conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": PROVISIONER_ROLE},
    ).scalar() is None:
        raise RuntimeError(
            f"the provisioner database role {PROVISIONER_ROLE!r} does not exist "
            "— this migration's grants have nothing to land on. See "
            "b1a0provrole for how each environment creates it."
        )
    op.execute(f"GRANT INSERT, SELECT ON {_TABLE} TO {PROVISIONER_ROLE}")
    op.execute(
        f"CREATE POLICY {_POLICY} ON {_TABLE} AS PERMISSIVE FOR ALL "
        f"TO {PROVISIONER_ROLE} USING (true) WITH CHECK (true)"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {_POLICY} ON {_TABLE}")
    op.execute(f"REVOKE INSERT, SELECT ON {_TABLE} FROM {PROVISIONER_ROLE}")
