-- L2 (Pillar L decision 2): the RLS-bound app database role, dev flavor.
--
-- CREATE ROLE is cluster-level, so it cannot live in the alembic chain —
-- the l2a0rlswall migration REFUSES to run until this role exists. The
-- compose postgres service mounts this file into
-- /docker-entrypoint-initdb.d (runs once, on a FRESH volume only), and
-- scripts/dev.sh re-applies it before every `alembic upgrade head` so
-- existing dev volumes converge; hence the idempotence guard.
--
-- LOGIN and nothing else: no SUPERUSER, no BYPASSRLS, no CREATEDB — the
-- whole point of the role is that FORCE ROW LEVEL SECURITY actually
-- applies to it. The password is local-dev-only, like compose's
-- usali/usali and admin/admin. Alembic keeps the owner role (usali).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'usali_app') THEN
    CREATE ROLE usali_app LOGIN PASSWORD 'usali_app';
  END IF;
END
$$;
