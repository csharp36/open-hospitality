"""b1e0signupalias on populated data: a property created through signup
before the fix (lowercase pms_source, no detection alias) comes out uppercase
with an alias named after it; a property that already has an alias, or has
no usable name, is left alone; and the backfill converges when run again."""

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


def _state(engine):
    with engine.begin() as conn:
        props = conn.execute(text(
            "SELECT property_id, pms_source FROM property"
        )).all()
        aliases = conn.execute(text(
            "SELECT org_id, property_id, pms_source, match_phrase "
            "FROM property_detection_alias"
        )).all()
    # Sets: the container's locale decides row order, not the migration.
    return {tuple(r) for r in props}, {tuple(r) for r in aliases}


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

            command.upgrade(cfg, "head")

            props, aliases = _state(engine)
            assert props == {
                ("HISJ", "OPERA"),
                ("blank-9f9f", "SKYTOUCH"),
                ("lakeside-test-lodge-ab12", "HOTELKEY"),
            }
            assert aliases == {
                (1, "HISJ", "OPERA", "HOLIDAY INN SAN JOSE"),  # untouched, not doubled
                (2, "lakeside-test-lodge-ab12", "HOTELKEY", "Lakeside Test Lodge"),
            }

            # Idempotent: the same statements against the migrated database
            # change nothing.
            with engine.begin() as conn:
                _mig.backfill(conn)
            assert _state(engine) == (props, aliases)
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous
