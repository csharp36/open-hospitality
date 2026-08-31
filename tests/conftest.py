import os

# Disable the Testcontainers "Ryuk" reaper sidecar. Our fixtures use
# `with PostgresContainer(...)`, which stops the container on scope exit, so the
# reaper is redundant here — and pulling its image is unreliable in sandboxed
# environments. Must be set BEFORE importing testcontainers. See:
# https://testcontainers-python.readthedocs.io/en/latest/#configuration
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import shutil  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from usali.db import make_engine, make_session_factory  # noqa: E402
from usali.ingestion import process_file  # noqa: E402
from usali.mapping.loader import load_mappings  # noqa: E402
from usali.mapping.property_registry import ensure_default_org, seed_properties  # noqa: E402
from usali.mapping.schedules import seed_schedules  # noqa: E402
from usali.models import Base  # noqa: E402
from usali.tenancy import FOUNDING_ORG_ID, OrgBoundSessionFactory  # noqa: E402

from tests.orgwall import (  # noqa: E402
    app_role_url,
    ensure_app_role,
    ensure_provisioner_role,
)
from tests.orgworld import build_two_tenant_world  # noqa: E402

SAMPLES = [
    "Trial Balance 07.07.2026 - Opera.pdf",
    "Autoclerk - Transaction Summary 07.07.2026.pdf",
    "Manager Flash 07.07.2026 - Opera.pdf",
    "Autoclerk - Manager Report 07.07.2026.pdf",
    "Market Code Statistics 07.07.2026 - Opera.pdf",
    "Autoclerk - Revenue by Rate Plan 07.07.2026.pdf",
]


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        url = pg.get_connection_url()
        # The RLS-bound app role, BEFORE migrating: l2a0rlswall refuses
        # to run without it (CREATE ROLE is cluster-level — dev init and
        # bootstrap own it in real environments; here the fixture does).
        ensure_app_role(url)
        ensure_provisioner_role(url)   # b1a0provrole refuses loudly without it
        os.environ["USALI_DB_URL"] = url
        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "migrations")
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield url


@pytest.fixture(scope="session")
def db_engine(db_url: str) -> Engine:
    return make_engine(db_url)


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    # Truncate all data tables before each test so committed rows from one test
    # don't leak into the next (the container/schema is session-scoped; tests
    # commit real data). Order/FKs handled by CASCADE; RESTART IDENTITY keeps
    # serial PKs deterministic per test.
    factory = make_session_factory(db_engine)
    with factory() as s:
        tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
        s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        s.commit()
        yield s
        s.rollback()


@pytest.fixture
def founding_org(db_session: Session) -> None:
    """The founding org row (L1): every tenant-owned table's org_id defaults
    to org 1 with an FK onto organization, so a world that writes ANY tenant
    row needs the org to exist — exactly as A2.1 guarantees in every real
    database. Find-or-creates THE SAME org as seed_properties
    (ensure_default_org), so a world using both holds exactly one org and it
    is id 1 — the one-org invariant the L2 walls assume. Worlds that build
    their own Organization keep doing so and must not also request this."""
    ensure_default_org(db_session)
    db_session.commit()


@pytest.fixture
def unconnected_org(db_session: Session) -> None:
    """Org 1 exists and is connected to NOTHING.

    `founding_org` alone is the wrong starting state for any test about a
    tenant's OWN connection state: `ensure_default_org` runs the D-OH17.15
    seed bridge, which UNCONDITIONALLY plants org 1's payroll (gusto) and
    accounting (qbo) rows from the process env — so "not connected" would be
    false before the test began, and a `_connect(... 'payroll' ...)` would
    collide with the seed on the (org_id, integration) primary key rather
    than testing anything. Deleting the seeded rows is the smallest fix that
    keeps ONE org-creating implementation (the L1 rule) instead of
    hand-rolling a second `Organization` insert here.

    The org row itself must stay: every `_connect` carries org_id = 1 and
    would otherwise die on `fk_org_integration_credential_org`.

    Shared between `tests/test_integrations.py` and `tests/test_checklist.py`
    (OH-17 Task 7) — one fixture, not two, the same reason `_connect` lives
    in one place."""
    ensure_default_org(db_session)
    # SCOPED to org 1 explicitly. `db_session` truncates first, so nothing
    # else can be here in-suite and the WHERE is redundant TODAY — but this
    # commit's headline lesson is that an org-scoped write carrying no org_id
    # is confined by RLS alone, and `db_session` runs as the superuser, which
    # bypasses RLS. An unscoped DELETE sitting in the same diff is the thing
    # a future two-org test copies.
    db_session.execute(
        text("DELETE FROM org_integration_credential WHERE org_id = :org"),
        {"org": FOUNDING_ORG_ID},
    )
    db_session.commit()


@pytest.fixture
def org_bound_factory(db_engine: Engine) -> OrgBoundSessionFactory:
    """A founding-org-bound SESSION FACTORY — the shape `usali.integrations`'
    `resolve_*` functions and `DbTokenStore` take (OH-17).

    Deliberately a factory and not a session: those callers open their OWN
    short session per call, so handing them one long-lived session would test
    a different object than production uses. Built exactly as
    `test_l2_rls_wall.test_the_app_factory_binds_the_founding_org` builds it,
    so what it wraps is the same L2-instrumented binding create_app uses.

    It does NOT depend on `db_session` or `founding_org`: it is only a
    factory, so it seeds nothing and truncates nothing. Tests pair it with
    whichever world fixture they need — and note `founding_org` unconditionally
    plants org 1's payroll and accounting credential rows (the D-OH17.15 seed
    bridge), which is the wrong starting state for any test about NOT being
    connected."""
    return OrgBoundSessionFactory(make_session_factory(db_engine), FOUNDING_ORG_ID)


@pytest.fixture(scope="module")
def app_role_engine(db_url: str) -> Iterator[Engine]:
    """The serving connection: the RLS-bound, non-owner `usali_app` role, so
    `FORCE ROW LEVEL SECURITY` genuinely applies (the testcontainers superuser
    bypasses RLS no matter the policy — which is why the tenancy wall pins
    connect as this role). Shared by the L7 walk and the L8 lenses."""
    engine = make_engine(app_role_url(db_url))
    yield engine
    engine.dispose()


@pytest.fixture
def two_tenant_world(
    db_session: Session, founding_org: None, app_role_engine: Engine
) -> object:
    """The shared two-org world (`tests/orgworld.py`): org 1 (founding) beside
    org 2 (provisioned + seeded through an org-2-bound app-role session).
    Returns the ids/subjects the tenancy tests assert against. L7 proved it
    isolated; L8's lenses attach cross-org probes to this same clean world."""
    return build_two_tenant_world(db_session, app_role_engine)


@pytest.fixture
def seed_six_pdfs(db_session: Session, tmp_path: Path) -> None:
    """Run all six sample PDFs through the real pipeline into the test database."""
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    load_mappings(db_session, "mapping/autoclerk.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in SAMPLES:
        shutil.copy(Path("docs/reference/samples") / name, inbox / name)

    for pdf in sorted(inbox.glob("*.pdf")):
        process_file(
            db_session, pdf,
            processed_dir=tmp_path / "processed", failed_dir=tmp_path / "failed",
        )
