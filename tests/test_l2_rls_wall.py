"""L2 (Pillar L decision 2): the two walls, each pinned STANDING ALONE.

*Application wall*: a per-session `do_orm_execute` hook adds
`with_loader_criteria(org_id == current)` to every ORM SELECT; a session
with no org context refuses loudly on its first org-scoped query.
*Database wall*: per-table RLS `USING (org_id = current_setting(
'app.org_id', ...))` with FORCE, connected as a non-owner role with no
BYPASSRLS. Either wall alone must stop a cross-org read — so the pins
here deliberately isolate them: the ORM-wall worlds run on the
SUPERUSER engine (which bypasses RLS entirely), and the DB-wall worlds
run raw SQL as the app role (no ORM hook anywhere in sight).

The structural inventory (every table policied + FORCEd, none sampled)
follows the L1 convention: sizes pinned as literals — the standing
differencing-oracle rule — and the table list imported from the
migration module itself so there is no transcription drift.
"""

import importlib.util
import os

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from usali.db import make_engine, make_session_factory  # noqa: E402
from usali.mapping.property_registry import ensure_default_org  # noqa: E402
from usali.models import Organization, Property, UsaliSchedule  # noqa: E402
from usali.tenancy import (  # noqa: E402
    APP_DB_ROLE,
    FOUNDING_ORG_ID,
    RLS_ORG_VAR,
    MissingOrgContext,
    OrgBoundSessionFactory,
    bind_org_context,
    instrument_org_wall,
)

from tests.orgwall import app_role_url, ensure_app_role  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "l2a0rlswall", "migrations/versions/l2a0rlswall_rls_wall.py"
)
assert _spec is not None and _spec.loader is not None
_l2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_l2)


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def app_role_engine(db_url):
    """The suite database, connected as the RLS-bound app role: LOGIN
    only, not the table owner, no BYPASSRLS — the role FORCE RLS
    actually applies to (the superuser fixture engine bypasses RLS no
    matter what the policies say)."""
    engine = make_engine(app_role_url(db_url))
    yield engine
    engine.dispose()


@pytest.fixture
def two_org_world(db_session):
    """Two orgs, one property each. Written by the SUPERUSER session
    (both walls bypassed), so the world exists regardless of what the
    walls under test do."""
    ensure_default_org(db_session)  # org 1, the founding org
    db_session.add(Organization(org_id=2, name="Rival Hotel Group"))
    db_session.flush()
    db_session.add(Property(
        property_id="WALL1", org_id=1, name="Org One Hotel",
        pms_source="opera",
    ))
    db_session.add(Property(
        property_id="WALL2", org_id=2, name="Org Two Hotel",
        pms_source="opera",
    ))
    db_session.commit()


# ------------------------------------------------- the application wall


def test_cross_org_orm_reads_come_back_empty(db_engine, two_org_world):
    """The ORM wall ALONE: on the superuser connection RLS is bypassed
    by definition, so anything filtered here was filtered by the
    `do_orm_execute` criteria and nothing else."""
    factory = make_session_factory(db_engine)
    with factory() as s:
        bind_org_context(s, 2)
        props = s.execute(select(Property)).scalars().all()
        assert [p.property_id for p in props] == ["WALL2"]
        # `organization` is org-scoped too: a tenant reads only its row.
        orgs = s.execute(select(Organization)).scalars().all()
        assert [o.org_id for o in orgs] == [2]
        # Session.get() rides the same execution path.
        assert s.get(Property, "WALL1") is None
        assert s.get(Property, "WALL2") is not None


def test_cross_org_aggregates_count_only_the_bound_org(db_engine, two_org_world):
    """The ORM wall on aggregate-only shapes: no org-scoped entity in the
    result columns — the entity appears only as the FROM (the labor and
    reporting modules' real shape). The count must see the bound org
    alone, and a contextless session must refuse the same shape rather
    than counting the world."""
    from sqlalchemy import func

    factory = make_session_factory(db_engine)
    with factory() as s:
        bind_org_context(s, 2)
        assert s.execute(
            select(func.count()).select_from(Property)
        ).scalar() == 1
        # The inferred-FROM flavor (no select_from at all).
        assert s.execute(
            select(func.count(Property.property_id))
        ).scalar() == 1
    with factory() as s:
        instrument_org_wall(s)
        with pytest.raises(MissingOrgContext):
            s.execute(select(func.count()).select_from(Property))


def test_join_only_entities_are_filtered(db_engine, two_org_world, db_session):
    """The ORM wall on entities that appear ONLY in a join, never in the
    result columns (the schedule/payroll surfaces' real shape). The
    cross-org row rides a single-column FK — exactly the FK-bypass shape
    l1a0orgid recorded as skipped, so only the walls stand between it and
    the join.

    This used to be staged on `department`->`property`. It cannot be any
    more: l9a0deptfk promoted that FK to the composite (org_id,
    property_id), so the intruder row is no longer representable at all
    and the INSERT is refused by the database (pinned in
    test_l9_review_remediation). `position`->`department` is still
    single-column — a position is not a money/tenancy seam, so L1's
    judgment stands there — which makes it the honest place to keep
    proving the wall. Same wall, same shape, a row that can still exist.
    """
    from usali.models import Department, Position

    dept = Department(org_id=1, property_id="WALL1", name="Rooms")
    db_session.add(dept)
    db_session.flush()
    db_session.add(Position(org_id=1, department_id=dept.department_id,
                            title="Attendant", flsa_exempt=False))
    # Org 2's position pointing at org 1's department: representable in
    # the schema (single-column FK, recorded skip), so the wall must
    # filter it out of org 1's join.
    db_session.add(Position(org_id=2, department_id=dept.department_id,
                            title="Intruder", flsa_exempt=False))
    db_session.commit()

    factory = make_session_factory(db_engine)
    with factory() as s:
        bind_org_context(s, 1)
        rows = s.execute(
            select(Department.name).join_from(
                Department, Position,
                Position.department_id == Department.department_id,
            )
        ).all()
        # One row per matching position: the org-2 position must NOT
        # match, even though its FK row joins cleanly.
        assert len(rows) == 1


def test_an_org_context_less_session_refuses_loudly(db_engine, two_org_world):
    """Instrumented but never bound: the first org-scoped query refuses
    with the dedicated error — never a silently unscoped result. Queries
    that touch no org-scoped table still work (the mapping dictionary
    and schedule list are platform reference data)."""
    factory = make_session_factory(db_engine)
    with factory() as s:
        instrument_org_wall(s)
        s.execute(select(UsaliSchedule)).all()  # org-free: fine
        with pytest.raises(MissingOrgContext):
            s.execute(select(Property))
        with pytest.raises(MissingOrgContext):
            s.execute(select(Organization))


def test_the_app_factory_binds_the_founding_org(db_engine, two_org_world):
    """The L2 transitional wiring `create_app` uses: every session the
    wrapped factory hands out is already bound — to FOUNDING_ORG_ID
    until L3 resolves the org from auth."""
    factory = OrgBoundSessionFactory(
        make_session_factory(db_engine), FOUNDING_ORG_ID
    )
    with factory() as s:
        props = s.execute(select(Property)).scalars().all()
        assert [p.property_id for p in props] == ["WALL1"]


# ---------------------------------------------------- the database wall


def test_the_db_wall_stands_alone_against_raw_sql(app_role_engine, two_org_world):
    """Raw SQL with the ORM hook nowhere in sight: RLS alone confines
    the read to the org the session variable names."""
    with app_role_engine.begin() as conn:
        conn.execute(
            text("SELECT set_config(:var, '2', true)"), {"var": RLS_ORG_VAR}
        )
        rows = conn.execute(text("SELECT property_id, org_id FROM property")).all()
        assert [(r.property_id, r.org_id) for r in rows] == [("WALL2", 2)]
        orgs = conn.execute(text("SELECT org_id FROM organization")).scalars().all()
        assert orgs == [2]


def test_an_unset_session_variable_yields_zero_rows(app_role_engine, two_org_world):
    """Fail-closed: no `SET LOCAL` at all → the policy compares against
    NULL → zero rows. Never 'unset means everything'."""
    with app_role_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM property")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM organization")).scalar() == 0


def test_an_empty_string_session_variable_yields_zero_rows(
    app_role_engine, two_org_world
):
    """The NULLIF pin: a custom GUC that has been set and expired can
    revert to the EMPTY STRING rather than NULL, and ''::int would make
    every later query on the pooled connection an ERROR instead of an
    empty result. The policy folds '' to NULL — zero rows, no error."""
    with app_role_engine.connect() as conn:
        try:
            conn.execute(
                text("SELECT set_config(:var, '', false)"),
                {"var": RLS_ORG_VAR},
            )
            assert conn.execute(
                text("SELECT count(*) FROM property")
            ).scalar() == 0
            assert conn.execute(
                text("SELECT count(*) FROM organization")
            ).scalar() == 0
        finally:
            # The set_config above is SESSION-level and this connection
            # returns to the pool — RESET it so no later test inherits
            # the '' state (kills the latent order-dependence).
            conn.execute(text(f"RESET {RLS_ORG_VAR}"))


def test_both_walls_compose_on_the_app_role_path(app_role_engine, two_org_world):
    """The full serving stack: app role + org-bound ORM session. The
    positive pin — rows DO come back — is what kills a dropped
    `SET LOCAL`: without it RLS fails closed to zero rows and this
    world sees nothing at all."""
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, 1)
        props = s.execute(select(Property)).scalars().all()
        assert [p.property_id for p in props] == ["WALL1"]
        # A COMMITTED transaction ends the SET LOCAL; the next
        # transaction must re-issue it (pooled connections never bleed
        # an org across transaction boundaries).
        s.commit()
        props = s.execute(select(Property)).scalars().all()
        assert [p.property_id for p in props] == ["WALL1"]


def test_with_check_refuses_a_cross_org_write(app_role_engine, two_org_world):
    """The policy's WITH CHECK side: a session bound to org 1 can write
    org 1 rows and CANNOT write (or re-home) an org 2 row — the wall
    holds for writes, not just reads."""
    with app_role_engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text("SELECT set_config(:var, '1', true)"), {"var": RLS_ORG_VAR}
            )
            conn.execute(text(
                "INSERT INTO property (property_id, org_id, name, pms_source) "
                "VALUES ('WALLW', 1, 'Write OK', 'opera')"
            ))
    with pytest.raises(ProgrammingError, match="row-level security"):
        with app_role_engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text("SELECT set_config(:var, '1', true)"),
                    {"var": RLS_ORG_VAR},
                )
                conn.execute(text(
                    "INSERT INTO property (property_id, org_id, name, pms_source) "
                    "VALUES ('WALLX', 2, 'Cross-org write', 'opera')"
                ))
    with pytest.raises(ProgrammingError, match="row-level security"):
        with app_role_engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text("SELECT set_config(:var, '1', true)"),
                    {"var": RLS_ORG_VAR},
                )
                conn.execute(text(
                    "UPDATE property SET org_id = 2 WHERE property_id = 'WALL1'"
                ))


def test_the_reference_tables_are_read_only_for_the_app_role(
    app_role_engine, db_session
):
    """The two unpoliced reference tables are the ONE place RLS does not
    stand — so the tenant-facing role must not be able to poison the
    shared PMS-code -> USALI curation. SELECT works (transform/reporting
    read them per request); every write refuses on privilege. The seed
    paths that write them run as the OWNER."""
    from usali.mapping.schedules import seed_schedules

    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    db_session.commit()
    with app_role_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM usali_schedule")
        ).scalar() > 0
        assert conn.execute(
            text("SELECT count(*) FROM usali_mapping_dictionary")
        ).scalar() == 0  # readable (empty in this world)
    for statement in (
        "INSERT INTO usali_schedule (usali_edition, schedule_id, name) "
        "VALUES (99, 99, 'poison')",
        "UPDATE usali_schedule SET name = 'poison'",
        "DELETE FROM usali_schedule",
        "INSERT INTO usali_mapping_dictionary "
        "(pms_source, pms_trx_code, usali_edition, usali_major_category) "
        "VALUES ('opera', 'POISON', 12, 'poison')",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            with app_role_engine.connect() as conn:
                with conn.begin():
                    conn.execute(text(statement))


def test_force_rls_filters_even_the_table_owner(db_engine, two_org_world):
    """What FORCE means: a non-superuser OWNER of the table is still
    filtered. Ownership is transferred to a scratch role inside a
    transaction that is ROLLED BACK (CREATE ROLE, ALTER OWNER and SET
    ROLE are all transactional), so the suite schema is untouched."""
    with db_engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("CREATE ROLE l2_force_probe"))
            conn.execute(text("ALTER TABLE property OWNER TO l2_force_probe"))
            conn.execute(text("SET ROLE l2_force_probe"))
            # Owner, unset variable: FORCE applies the policy → zero rows.
            assert conn.execute(
                text("SELECT count(*) FROM property")
            ).scalar() == 0
            # Owner, org 2: sees exactly org 2 — filtered, not exempt.
            conn.execute(
                text("SELECT set_config(:var, '2', true)"), {"var": RLS_ORG_VAR}
            )
            rows = conn.execute(text("SELECT property_id FROM property")).scalars().all()
            assert rows == ["WALL2"]
        finally:
            trans.rollback()


# --------------------------------------------- the migration, structurally


def test_l2_is_on_the_single_alembic_lineage():
    """One head always; l2a0rlswall is IN the lineage (the exact-head pin
    moved to test_l3_org_resolution when L3 stacked on top)."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert any(
        rev.revision == "l2a0rlswall"
        for rev in script.walk_revisions("base", heads[0])
    )


def test_the_wall_constants_are_pinned_as_literals():
    """The role name and session-variable name are contracts shared by
    the migration, the session hook, dev init and bootstrap — pinned as
    literals so no refactor can silently move everyone at once."""
    assert APP_DB_ROLE == "usali_app"
    assert RLS_ORG_VAR == "app.org_id"
    assert FOUNDING_ORG_ID == 1


def test_the_rls_inventory_is_complete_and_forced(db_engine):
    """Every org-scoped table — no sampling — carries the org_wall
    policy, ENABLE and FORCE. The size is a literal (44 = L1's 42 +
    property + organization): the migration's own tuple is the source
    for NAMES only, never for the count. L5 adds one more org-scoped
    table (org_settings) with its OWN RLS in the l5a0orgsettings
    migration — enumerated here so the inventory stays exact, not sampled.
    m1a0propcfg adds three more (room_inventory, out_of_order_room,
    fiscal_calendar), each with its own RLS, for the same reason."""
    assert len(_l2.RLS_TABLES) == 44
    # The full org-scoped inventory at head = L2's 44 + L5's org_settings +
    # m1a0propcfg's three property-config tables.
    expected = set(_l2.RLS_TABLES) | {
        "org_settings", "room_inventory", "out_of_order_room", "fiscal_calendar",
    }
    with db_engine.connect() as conn:
        policied = {
            r[0] for r in conn.execute(text(
                "SELECT tablename FROM pg_policies "
                "WHERE schemaname = 'public' AND policyname = 'org_wall'"
            ))
        }
        forced = {
            r[0] for r in conn.execute(text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'r' "
                "AND c.relrowsecurity AND c.relforcerowsecurity"
            ))
        }
        quals = conn.execute(text(
            "SELECT qual, with_check FROM pg_policies "
            "WHERE schemaname = 'public' AND policyname = 'org_wall'"
        )).all()
        role = conn.execute(text(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
            "FROM pg_roles WHERE rolname = :r"
        ), {"r": APP_DB_ROLE}).one()
    # The role holds NO cluster powers at head. The fixture creates it
    # CLOUD-shaped (CREATEROLE/CREATEDB — what gcloud stamps), so this
    # pins the migration's strip actually running, not a lucky default.
    assert tuple(role) == (False, False, False, False)
    assert policied == expected
    assert forced == expected
    # The enumerated exclusions stay unpolicied — org-free reference data.
    for excluded in ("usali_schedule", "usali_mapping_dictionary", "alembic_version"):
        assert excluded not in policied
    # Every policy reads THE session variable, both directions (USING +
    # WITH CHECK).
    for qual, with_check in quals:
        assert RLS_ORG_VAR in qual and "current_setting" in qual
        assert with_check is not None and RLS_ORG_VAR in with_check


def test_the_migration_refuses_without_the_app_role():
    """CREATE ROLE is cluster-level and belongs to dev init / bootstrap;
    the migration refuses LOUDLY (naming them) rather than inventing
    cluster state. L1 itself must migrate fine without the role."""
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            os.environ["USALI_DB_URL"] = url
            cfg = Config("alembic.ini")
            cfg.set_main_option("script_location", "migrations")
            cfg.set_main_option("sqlalchemy.url", url)
            command.upgrade(cfg, "l1a0orgid")  # no role needed below L2
            with pytest.raises(RuntimeError, match=APP_DB_ROLE):
                command.upgrade(cfg, "head")
            # bootstrap/dev-init are NAMED in the refusal.
            with pytest.raises(RuntimeError, match="bootstrap"):
                command.upgrade(cfg, "head")
            # ... and creating the role makes the same upgrade converge.
            ensure_app_role(url)
            command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous
