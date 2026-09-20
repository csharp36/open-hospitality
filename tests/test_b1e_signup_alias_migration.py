"""b1e0signupalias on populated data: a property created through signup
before the fix (lowercase pms_source, no detection alias) comes out uppercase
with an alias named after it; a property that already has an alias, or has
no usable name, is left alone; the backfill converges when run again; and --
the part the cloud migrate job depends on -- it changes rows when run as a
NON-superuser table owner, because both tables are FORCE RLS and the bare
statements would otherwise be a silent no-op for that identity."""

import importlib.util
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from tests.orgwall import ensure_app_role, ensure_provisioner_role  # noqa: E402

_PRE = "n2a0nightadjust"

_spec = importlib.util.spec_from_file_location(
    "b1e0signupalias",
    "migrations/versions/b1e0signupalias_signup_property_source_and_alias.py",
)
assert _spec is not None and _spec.loader is not None
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)


def _cfg(url: str) -> Config:
    # env.py reads USALI_DB_URL (the test_migration_on_populated_data
    # convention; the caller restores the previous value).
    os.environ["USALI_DB_URL"] = url
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _state_on(conn):
    props = conn.execute(text(
        "SELECT property_id, pms_source FROM property"
    )).all()
    aliases = conn.execute(text(
        "SELECT org_id, property_id, pms_source, match_phrase "
        "FROM property_detection_alias"
    )).all()
    # Sets: the container's locale decides row order, not the migration.
    return {tuple(r) for r in props}, {tuple(r) for r in aliases}


def _forced(conn) -> dict[str, bool]:
    return dict(conn.execute(text(
        "SELECT relname, relforcerowsecurity FROM pg_class "
        "WHERE relname IN ('property', 'property_detection_alias')"
    )).all())


_PLANTED = (
    {("HISJ", "OPERA"), ("blank-9f9f", "skytouch"), ("lakeside-test-lodge-ab12", "hotelkey")},
    {(1, "HISJ", "OPERA", "HOLIDAY INN SAN JOSE")},
)
_MIGRATED = (
    {("HISJ", "OPERA"), ("blank-9f9f", "SKYTOUCH"), ("lakeside-test-lodge-ab12", "HOTELKEY")},
    {
        (1, "HISJ", "OPERA", "HOLIDAY INN SAN JOSE"),  # untouched, not doubled
        (2, "lakeside-test-lodge-ab12", "HOTELKEY", "Lakeside Test Lodge"),
    },
)


def _state(engine):
    with engine.begin() as conn:
        return _state_on(conn)


def test_signup_properties_are_uppercased_and_aliased_once():
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            ensure_app_role(url)
            ensure_provisioner_role(url)
            cfg = _cfg(url)
            command.upgrade(cfg, _PRE)
            engine = create_engine(url)
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO organization (org_id, kc_org_alias, name) "
                    "VALUES (1, 'pilot', 'Org'), (2, 'lakeside', 'Lakeside')"
                ))
                conn.execute(text(
                    "INSERT INTO property (property_id, org_id, name, pms_source) VALUES "
                    # what signup wrote before the fix, in a second org
                    "('lakeside-test-lodge-ab12', 2, 'Lakeside Test Lodge', 'hotelkey'), "
                    # a registry-seeded property: already uppercase, already aliased
                    "('HISJ', 1, 'HOLIDAY INN SAN JOSE', 'OPERA'), "
                    # lowercase but unnamed: uppercased, never aliased
                    "('blank-9f9f', 1, '   ', 'skytouch')"
                ))
                conn.execute(text(
                    "INSERT INTO property_detection_alias "
                    "(org_id, property_id, pms_source, match_phrase) "
                    "VALUES (1, 'HISJ', 'OPERA', 'HOLIDAY INN SAN JOSE')"
                ))

            assert _state(engine) == _PLANTED

            # As the cloud job runs it: a non-superuser OWNER of both tables
            # (test_force_rls_filters_even_the_table_owner's technique --
            # CREATE ROLE, ALTER OWNER and SET ROLE inside one transaction
            # that is rolled back, so the suite schema is untouched).
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text("CREATE ROLE b1e_owner_probe"))
                    for table in ("property", "property_detection_alias"):
                        conn.execute(text(f"ALTER TABLE {table} OWNER TO b1e_owner_probe"))
                    # FORCE filters the owner's reads too, so every read
                    # below steps back to the superuser (RESET ROLE) and the
                    # next migration call steps into the owner again.
                    conn.execute(text("SET ROLE b1e_owner_probe"))
                    # The bare statements under FORCE RLS: the owner sees no
                    # rows, so nothing changes -- the silent no-op the
                    # migration exists to avoid.
                    _mig.backfill(conn)
                    conn.execute(text("RESET ROLE"))
                    assert _state_on(conn) == _PLANTED
                    # The migration body: lifts FORCE, backfills, re-forces.
                    conn.execute(text("SET ROLE b1e_owner_probe"))
                    _mig.run(conn)
                    conn.execute(text("RESET ROLE"))
                    assert _state_on(conn) == _MIGRATED
                    assert _forced(conn) == {
                        "property": True, "property_detection_alias": True}
                finally:
                    trans.rollback()
            assert _state(engine) == _PLANTED  # the probe run left nothing behind

            command.upgrade(cfg, "head")

            assert _state(engine) == _MIGRATED
            with engine.begin() as conn:
                assert _forced(conn) == {
                    "property": True, "property_detection_alias": True}

            # Idempotent: the same statements against the migrated database
            # change nothing.
            with engine.begin() as conn:
                _mig.backfill(conn)
            assert _state(engine) == _MIGRATED
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous
