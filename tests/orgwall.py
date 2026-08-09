"""Shared L2 test plumbing: the app database role in the test container.

`CREATE ROLE` is cluster-level, so it lives OUTSIDE the migration chain —
the dev compose init (`scripts/dev_pg_init.sql`) and the cloud bootstrap
(`scripts/cloud/bootstrap.sh`) own it in real environments, and the
`l2a0rlswall` migration REFUSES to run when the role is missing rather
than inventing cluster state. Test fixtures that migrate a fresh
container therefore create the role first, exactly as those environments
do: no SUPERUSER, no BYPASSRLS, not the table owner — so `FORCE ROW
LEVEL SECURITY` genuinely applies to it (the testcontainers superuser
bypasses RLS no matter what, which is why the DB-wall pins must connect
as this role). Created CLOUD-shaped (LOGIN CREATEROLE CREATEDB — what
`gcloud sql users create` stamps) so the migration's power-strip runs
in every container; at head the role is LOGIN only.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from usali.tenancy import APP_DB_ROLE

APP_ROLE_PASSWORD = "usali-app-test"


def ensure_app_role(url: str) -> None:
    """Create the RLS-bound app role (idempotently) before migrating."""
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": APP_DB_ROLE},
            ).scalar()
            if exists is None:
                # CREATEROLE/CREATEDB on purpose — the CLOUD shape, not
                # the dev one: `gcloud sql users create` stamps both (via
                # cloudsqlsuperuser membership), and creating the test
                # role the same way makes the migration's power-strip
                # path execute in every migrated container, where the
                # inventory pin asserts the attributes are GONE at head.
                conn.execute(text(
                    f"CREATE ROLE {APP_DB_ROLE} LOGIN CREATEROLE CREATEDB "
                    f"PASSWORD '{APP_ROLE_PASSWORD}'"
                ))
    finally:
        engine.dispose()


def app_role_url(superuser_url: str) -> str:
    """The same database, connected as the RLS-bound app role."""
    return make_url(superuser_url).set(
        username=APP_DB_ROLE, password=APP_ROLE_PASSWORD
    ).render_as_string(hide_password=False)
