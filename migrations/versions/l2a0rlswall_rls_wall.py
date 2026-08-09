"""L2: the database wall — per-table RLS + the app role's grants.

Pillar L decision 2, the second of two independent walls (the first is
the `do_orm_execute` criteria hook in `usali.tenancy` — either alone
must stop a cross-org leak). Per org-scoped table:

    ALTER TABLE t ENABLE ROW LEVEL SECURITY;
    ALTER TABLE t FORCE ROW LEVEL SECURITY;
    CREATE POLICY org_wall ON t
        USING (org_id = NULLIF(current_setting('app.org_id', true), '')::int)
        WITH CHECK (same);

- The session variable is transaction-local: `usali.tenancy._db_wall`
  issues `set_config('app.org_id', <org>, true)` (= SET LOCAL) at every
  transaction begin, so pooled connections cannot bleed an org across
  requests. A transaction that never set it compares against NULL and
  reads ZERO rows — fail-closed, never fail-open.
- The NULLIF wrapper: `current_setting(name, true)` returns NULL for a
  variable never defined on the connection, but a custom GUC that HAS
  been SET LOCAL and then expired can revert to the EMPTY STRING, and
  ''::int is an error, not an empty result. NULLIF folds both unset
  shapes to NULL → zero rows, deterministically, on fresh and reused
  pooled connections alike.
- WITH CHECK is spelled explicitly (Postgres would default it to the
  USING expression for a FOR ALL policy): a session bound to org A can
  neither insert a row claiming org B nor re-home a row across the
  wall. While exactly one org exists this is the wall the L1 server
  default ('1') was bridging toward — the default cannot encode a wrong
  decision anymore, because a disagreeing write refuses here.
- FORCE: without it the table OWNER bypasses policies entirely. The
  cloud/dev owner (`usali`) is not a superuser, so FORCE is what makes
  "alembic keeps the owner role" safe: the owner still MIGRATES freely
  (DDL is not row access) but cannot silently read across orgs at query
  time. Superusers bypass RLS regardless of FORCE — which is why no
  serving path may ever connect as one.

## The app role (usali_app)

The serving paths connect as a NEW role: LOGIN only — no SUPERUSER, no
BYPASSRLS, not the table owner. CREATE ROLE is cluster-level, so it
cannot live in this (database-scoped) migration chain; each environment
provisions it and this migration REFUSES loudly when it is missing
rather than inventing cluster state:

- dev: `scripts/dev_pg_init.sql` (compose initdb mount; `scripts/dev.sh
  start` re-applies it so existing volumes converge),
- cloud: `scripts/cloud/bootstrap.sh` (ensure_sql_user, K2 posture),
- tests: `tests/orgwall.ensure_app_role` (the fixtures create it before
  migrating, exactly as the environments above do).

Grants: table DML + sequence usage on everything existing, DEFAULT
PRIVILEGES for everything future (so later migrations need no grant
boilerplate), and explicit REVOKEs where least privilege bites:
`alembic_version` entirely (the migration ledger belongs to the owner
alone), and INSERT/UPDATE/DELETE on the two org-free reference tables
(`usali_schedule`, `usali_mapping_dictionary`) — they carry NO policy
(platform curation shared by every tenant, the judgment recorded in
`l1a0orgid`), which makes them the one place RLS does not stand, so the
tenant-facing role gets SELECT only; the seed paths that write them run
as the owner. The migration also strips CREATEROLE/CREATEDB if the
provisioning path stamped them (Cloud SQL does, via cloudsqlsuperuser
membership) — the app role holds NO cluster powers anywhere.

The table list derives from `l1a0orgid._ORG_TABLES` (no transcription
drift) plus the two tables that module handles separately: `property`
(org_id since A2.1) and `organization` itself — org-scoped BY ITS OWN
PRIMARY KEY, so a tenant reads exactly its own row.

Downgrade drops the policies, un-FORCEs and disables RLS, and revokes
the grants (current and default). It does NOT drop the role: the role
is cluster state owned by dev-init/bootstrap (the K2 create-only
posture), and another database in the cluster may reference it.

The role name and variable name are imported from `usali.tenancy` (the
one-predicate-one-function rule; `migrations/env.py` already imports
the app's config the same way). Both are contractual constants pinned
as literals in `tests/test_l2_rls_wall.py`, so they cannot drift under
this frozen migration.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic import op

from usali.tenancy import APP_DB_ROLE, RLS_ORG_VAR

revision = "l2a0rlswall"
down_revision = "l1a0orgid"
branch_labels = None
depends_on = None


def _l1() -> ModuleType:
    path = Path(__file__).with_name("l1a0orgid_org_id_denormalization.py")
    spec = importlib.util.spec_from_file_location("l1a0orgid", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 44 = L1's 42 denormalized tables + property + organization.
RLS_TABLES: tuple[str, ...] = tuple(_l1()._ORG_TABLES) + (
    "property",
    "organization",
)

_POLICY = "org_wall"
_PREDICATE = (
    f"org_id = NULLIF(current_setting('{RLS_ORG_VAR}', true), '')::int"
)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
        {"role": APP_DB_ROLE},
    ).scalar() is None:
        raise RuntimeError(
            f"the app database role {APP_DB_ROLE!r} does not exist — this "
            "migration's RLS grants have nothing to land on. CREATE ROLE is "
            "cluster-level and lives outside the migration chain: "
            "scripts/cloud/bootstrap.sh provisions it in the cloud, "
            "scripts/dev_pg_init.sql (mounted into compose initdb and "
            "re-applied by scripts/dev.sh start) in dev, and "
            "tests/orgwall.ensure_app_role in the test container. Create the "
            "role, then re-run alembic."
        )

    # Strip cluster powers the provisioning path may have stamped on the
    # role: Cloud SQL creates every `gcloud sql users create` user as a
    # member of cloudsqlsuperuser WITH the CREATEROLE/CREATEDB attributes
    # on the role itself — powers the dev init deliberately withholds
    # (and its pin forbids), so without this dev and cloud would disagree
    # silently. Conditional on purpose: a bare dev/test-shaped role skips
    # the ALTER entirely (nothing to strip, no permission needed). If the
    # cloud owner lacks ADMIN on the app role (PG16 tightened CREATEROLE),
    # this fails LOUDLY and the operator runs the ALTER as postgres —
    # never silently leaving the powers in place. SUPERUSER/BYPASSRLS
    # cannot be stamped by any provisioning path here (Cloud SQL grants
    # neither; dev init and the test fixture create LOGIN only).
    powers = conn.execute(
        sa.text(
            "SELECT rolcreaterole OR rolcreatedb FROM pg_roles "
            "WHERE rolname = :role"
        ),
        {"role": APP_DB_ROLE},
    ).scalar()
    if powers:
        op.execute(f"ALTER ROLE {APP_DB_ROLE} NOCREATEROLE NOCREATEDB")

    # The app role's privileges: DML on today's tables, and DEFAULT
    # PRIVILEGES (recorded for the owner running this migration) so
    # tables created by FUTURE migrations arrive already granted.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
        f"public TO {APP_DB_ROLE}"
    )
    op.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_DB_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, "
        f"UPDATE, DELETE ON TABLES TO {APP_DB_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON "
        f"SEQUENCES TO {APP_DB_ROLE}"
    )
    # The migration ledger belongs to the owner alone.
    op.execute(f"REVOKE ALL ON alembic_version FROM {APP_DB_ROLE}")
    # The two unpoliced reference tables are the ONE place RLS does not
    # stand — a tenant-facing write there would poison the shared
    # PMS-code -> USALI curation for every tenant. The app role reads
    # them (transform/reporting resolve mappings per request) and never
    # writes them: the only writers are the seed paths (cli.py
    # seed-schedules / load-mappings, demo_seed), which run as the OWNER.
    for table in ("usali_schedule", "usali_mapping_dictionary"):
        op.execute(
            f"REVOKE INSERT, UPDATE, DELETE ON {table} FROM {APP_DB_ROLE}"
        )

    for table in RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {_POLICY} ON {table} "
            f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    for table in reversed(RLS_TABLES):
        op.execute(f"DROP POLICY {_POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, "
        f"UPDATE, DELETE ON TABLES FROM {APP_DB_ROLE}"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON "
        f"SEQUENCES FROM {APP_DB_ROLE}"
    )
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_DB_ROLE}")
    op.execute(
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_DB_ROLE}"
    )
    # The role itself is cluster state owned by dev-init/bootstrap
    # (create-only, K2 posture) — deliberately NOT dropped here.
