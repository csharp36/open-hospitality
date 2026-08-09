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

from tests.orgwall import app_role_url, ensure_app_role  # noqa: E402
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
