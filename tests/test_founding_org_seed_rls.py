"""The founding-org seed must succeed under FORCE RLS as a NON-superuser role.

The demo seed runs on the owner role in Cloud SQL, which — unlike the
testcontainers/dev superuser — does NOT bypass `FORCE ROW LEVEL SECURITY`.
`ensure_default_org` therefore runs on a founding-BOUND session (app.org_id=1),
and the DB wall's WITH CHECK demands the new `organization` row's org_id EQUAL
that bound org. A bare autoincrement can hand out a different id (a prior failed
insert already burned the sequence's 1; nextval does not roll back), which RLS
refuses. The every-other-test superuser bypasses RLS, so this only ever
surfaced in the cloud — this test reproduces the cloud shape by connecting as
the RLS-bound app role and ADVANCING the sequence first, so a regression back to
autoincrement fails here exactly as it did in Cloud SQL.
"""

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from testcontainers.postgres import PostgresContainer

from tests.orgwall import app_role_url, ensure_app_role, ensure_provisioner_role
from usali.db import make_engine, make_session_factory
from usali.mapping.property_registry import ensure_default_org
from usali.models import Organization
from usali.tenancy import FOUNDING_ORG_ID, bind_org_context

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


def _cfg(url: str) -> Config:
    # migrations/env.py reads USALI_DB_URL, not the config's sqlalchemy.url.
    os.environ["USALI_DB_URL"] = url
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_founding_org_seeds_under_force_rls_as_the_app_role():
    """Own throwaway container (the RLS-shape convention): migrate to head with
    the app role present, ADVANCE the organization sequence past 1, then seed
    the founding org as the RLS-bound app role on a founding-bound session. The
    row must land as org FOUNDING_ORG_ID — proof the seed uses an EXPLICIT id,
    not the (now-advanced) autoincrement the DB wall's WITH CHECK would reject.
    """
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            ensure_app_role(url)
            ensure_provisioner_role(url)   # b1a0provrole refuses without it too
            command.upgrade(_cfg(url), "head")

            # Simulate the cloud state that broke autoincrement: the sequence's
            # value 1 is already spent (a prior failed insert / any prior org),
            # so nextval would now yield != FOUNDING_ORG_ID.
            owner = make_engine(url)
            with owner.begin() as conn:
                conn.execute(text(
                    "SELECT setval(pg_get_serial_sequence('organization','org_id'), 5)"
                ))
                # In cloud the seed runs as the OWNER, which owns the identity
                # sequence and can setval it. The app role stands in for that
                # RLS-bound owner here, so grant it the same sequence write the
                # real owner has inherently (usali_app otherwise holds only
                # USAGE, SELECT on sequences).
                seq = conn.execute(text(
                    "SELECT pg_get_serial_sequence('organization','org_id')"
                )).scalar_one()
                conn.execute(text(f"GRANT UPDATE ON SEQUENCE {seq} TO usali_app"))
            owner.dispose()

            # Seed as the RLS-bound app role (FORCE RLS genuinely applies), on a
            # session bound to the founding org — exactly the cloud seed's shape.
            app_engine = make_engine(app_role_url(url))
            factory = make_session_factory(app_engine)
            session = bind_org_context(factory(), FOUNDING_ORG_ID)
            try:
                org_id = ensure_default_org(session)
                session.commit()
                assert org_id == FOUNDING_ORG_ID
            finally:
                session.close()
                app_engine.dispose()

            # Confirm via the owner (bypasses RLS) that exactly one org exists,
            # and it is the founding id — not a sequence-driven 6.
            owner = make_engine(url)
            with owner.connect() as conn:
                ids = conn.execute(
                    select(Organization.org_id).order_by(Organization.org_id)
                ).scalars().all()
            owner.dispose()
            assert ids == [FOUNDING_ORG_ID]
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous


def test_reseed_does_not_regress_the_sequence_past_provisioned_tenants():
    """A RE-seed must never hand the next tenant an id that already exists.

    `ensure_default_org` advances the sequence past the explicit founding id,
    but it computed that target from `max(org_id) FROM organization` — a SELECT
    the `org_wall` policy filters, because `organization` carries FORCE RLS and
    the cloud seed runs as a non-superuser owner that does NOT bypass it. The
    seed session is bound to the founding org, so it sees org 1 and nothing
    else: `GREATEST(1, 1)` = 1, and the next `nextval` yields 2 — an id a
    self-serve signup already took.

    `job.sh` re-seeds on EVERY deploy, so this regressed the sequence every
    time, and `POST /api/signup/complete` 500'd on `organization_pkey` for the
    next stranger through the door. Live failure 2026-08-27.

    Every other test in the suite runs as the testcontainers SUPERUSER, which
    bypasses RLS, sees the true max, and passes — which is why only Cloud SQL
    ever showed this. This test connects as the RLS-bound app role to reproduce
    the cloud shape.
    """
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            ensure_app_role(url)
            ensure_provisioner_role(url)
            command.upgrade(_cfg(url), "head")

            owner = make_engine(url)
            with owner.begin() as conn:
                seq = conn.execute(text(
                    "SELECT pg_get_serial_sequence('organization','org_id')"
                )).scalar_one()
                conn.execute(text(f"GRANT UPDATE ON SEQUENCE {seq} TO usali_app"))
            owner.dispose()

            def reseed() -> None:
                """One deploy's worth of seeding, in the cloud's RLS shape."""
                app_engine = make_engine(app_role_url(url))
                session = bind_org_context(
                    make_session_factory(app_engine)(), FOUNDING_ORG_ID
                )
                try:
                    ensure_default_org(session)
                    session.commit()
                finally:
                    session.close()
                    app_engine.dispose()

            reseed()  # first deploy: founding org lands as id 1

            # A stranger signs up. provision_tenant inserts with the
            # AUTOINCREMENT (no explicit id), as the superuser here since the
            # provisioner path is not what is under test.
            owner = make_engine(url)
            with owner.begin() as conn:
                tenant_id = conn.execute(text(
                    "INSERT INTO organization (name, kc_org_alias) "
                    "VALUES ('Tenant Two', 'tenant-two') RETURNING org_id"
                )).scalar_one()
            owner.dispose()
            assert tenant_id == FOUNDING_ORG_ID + 1, "tenant should follow the founding org"

            reseed()  # the NEXT deploy re-runs the same seed

            # The invariant: whatever the seed does, the sequence must still be
            # ahead of every id in the table. Before the fix this handed back
            # `tenant_id` itself and the next signup died on organization_pkey.
            owner = make_engine(url)
            with owner.connect() as conn:
                true_max = conn.execute(
                    text("SELECT max(org_id) FROM organization")
                ).scalar_one()
                nxt = conn.execute(text(f"SELECT nextval('{seq}')")).scalar_one()
            owner.dispose()
            assert nxt > true_max, (
                f"re-seed regressed the sequence: next id {nxt} collides with "
                f"an existing org (max {true_max})"
            )
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous
