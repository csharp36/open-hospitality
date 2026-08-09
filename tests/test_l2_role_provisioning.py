"""L2: app-role provisioning pins — dev compose init, dev.sh, bootstrap,
deploy.

`CREATE ROLE` is cluster-level, so it cannot live in the migration
chain; each environment grows the role in its own idiom and the
`l2a0rlswall` migration refuses to run until one of them has. Shell
gets the K2 treatment: parse gates plus content pins on the properties
carrying the posture — the role is LOGIN-only (no SUPERUSER, no
BYPASSRLS — a bypassing app role would quietly hollow out the DB wall),
bootstrap stays CREATE-ONLY and idempotent, secret values ride stdin,
the cloud SERVING path connects as the app role while the migrate-seed
job KEEPS the owner (alembic owns the schema; FORCE keeps even the
owner inside the wall at query time).
"""

import subprocess
from pathlib import Path

from usali.tenancy import APP_DB_ROLE

REPO = Path(__file__).parent.parent
BOOTSTRAP = (REPO / "scripts" / "cloud" / "bootstrap.sh").read_text()
DEPLOY = (REPO / "scripts" / "cloud" / "deploy_app.sh").read_text()
ENV = (REPO / "scripts" / "cloud" / "env.sh").read_text()
COMPOSE = (REPO / "docker-compose.yml").read_text()
DEV = (REPO / "scripts" / "dev.sh").read_text()
INIT = (REPO / "scripts" / "dev_pg_init.sql").read_text()


def test_the_touched_scripts_still_parse():
    for path in ("scripts/dev.sh", "scripts/cloud/bootstrap.sh",
                 "scripts/cloud/deploy_app.sh", "scripts/cloud/env.sh",
                 "scripts/cloud/job.sh"):
        subprocess.run(["bash", "-n", str(REPO / path)], check=True)


# ------------------------------------------------------------------ dev


def test_dev_compose_mounts_the_role_init():
    """The postgres image runs /docker-entrypoint-initdb.d on a FRESH
    volume — the init file rides that mount."""
    assert "dev_pg_init.sql" in COMPOSE
    assert "/docker-entrypoint-initdb.d/10-usali-app-role.sql" in COMPOSE


def test_the_dev_init_creates_a_login_only_role():
    """LOGIN and nothing else. SUPERUSER or BYPASSRLS on the app role
    would hollow out the DB wall while every test kept passing."""
    assert f"CREATE ROLE {APP_DB_ROLE} LOGIN" in INIT
    assert "pg_roles" in INIT  # the idempotence guard (re-runs no-op)
    sql = "\n".join(
        line for line in INIT.splitlines()
        if not line.strip().startswith("--")
    )
    for attribute in ("SUPERUSER", "BYPASSRLS", "CREATEDB", "CREATEROLE"):
        assert attribute not in sql, f"{attribute} on the app role"


def test_dev_start_converges_existing_volumes_before_migrating():
    """initdb scripts never re-run on an existing volume, and the
    l2a0rlswall migration refuses without the role — so dev.sh
    re-applies the (idempotent) init BEFORE alembic on every start."""
    assert "10-usali-app-role.sql" in DEV
    assert DEV.index("10-usali-app-role.sql") < DEV.index("alembic upgrade head")


# ---------------------------------------------------------------- cloud


def test_bootstrap_grows_the_app_role_idempotently():
    """The K2 posture verbatim: a new secret through ensure_secret
    (value generated in-process, stdin only) and a new SQL user through
    ensure_sql_user (describe-or-create) — no deletes anywhere, which
    test_k2's create-only sweep re-checks across the whole script."""
    assert "ensure_secret usali-app-db-password gen_password" in BOOTSTRAP
    assert f"ensure_sql_user {APP_DB_ROLE} usali-app-db-password" in BOOTSTRAP


def test_the_app_sa_can_read_the_app_role_secret():
    """The new password rides the SECRETS accessor loop — the runtime
    SA gets a per-secret binding, not a project-wide role."""
    secrets_block = BOOTSTRAP.split("SECRETS=(", 1)[1].split(")", 1)[0]
    assert "usali-app-db-password" in secrets_block


def test_the_serving_revision_connects_as_the_app_role():
    """The SERVING path is the one that faces tenants: it connects as
    the RLS-bound role — non-owner, no BYPASSRLS — so the DB wall
    actually applies to every request."""
    assert 'APP_SECRETS="USALI_DB_PASSWORD=usali-app-db-password:latest' in DEPLOY
    app_block = DEPLOY.split("run deploy usali-app", 1)[1]
    assert f"USALI_DB_USER={APP_DB_ROLE}" in app_block
    assert '--set-secrets "${APP_SECRETS}"' in app_block


def test_the_migrate_seed_job_keeps_the_owner_role():
    """Alembic owns the schema (DDL, grants, policies): the job keeps
    the owner. Its section neither switches user nor mounts the app
    password."""
    assert 'JOB_SECRETS="USALI_DB_PASSWORD=usali-db-password:latest' in DEPLOY
    job_block = DEPLOY.split("jobs deploy usali-migrate-seed", 1)[1] \
        .split("run deploy usali-app", 1)[0]
    assert '--set-secrets "${JOB_SECRETS}"' in job_block
    assert APP_DB_ROLE not in job_block


def test_env_composes_the_url_from_the_selected_user():
    """One predicate, one function (the K4 pin's point, extended): both
    entrypoints still source env.sh; the user is now selectable there —
    defaulting to the owner — so the job and the service compose the
    SAME URL shape and differ only in the injected identity."""
    assert "USALI_DB_USER" in ENV
    assert '"usali"' in ENV  # the owner default, for the job and dev
