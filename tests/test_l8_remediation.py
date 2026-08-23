"""L8 three-lens remediation pins — assert the FIXED (post-fix) behavior.

The migration/disclosure/money lenses left reproductions asserting the BUGGY
behavior; these invert them. Every cross-org claim runs on the RLS-BOUND
`usali_app` role (`app_role_engine` + `two_tenant_world`), never the
testcontainers superuser (which bypasses RLS and would make a leak look
sealed). F1 owns its own container (it exercises the migration chain).

Findings, severity order:
  F1 HIGH      — the L1 downgrade now REFUSES a multi-org database (symmetric
                 to the upgrade guard), atomically — no tenancy is torn down.
  F2 MED-HIGH  — pay_schedule / labor_standard composite FK makes a cross-org
                 unique squat structurally impossible.
  F3 MED       — the ingestion stage path stamps org_id from the session, so
                 an org != 1 ingest lands in its own org (not a 500, not org 1).
  F4 LOW-MED   — the four *_stage uniques carry org_id, so a cross-org
                 row_hash collision keeps BOTH orgs' rows.
  F5 LOW       — resolve_scope intersects the token scopes claim with the
                 active org's visible properties.
"""

import os
from datetime import date
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.postgres import PostgresContainer  # noqa: E402

from tests.orgwall import ensure_app_role, ensure_provisioner_role  # noqa: E402
from usali.auth import Principal  # noqa: E402
from usali.db import make_session_factory  # noqa: E402
from usali.models import (  # noqa: E402
    Department,
    LaborStandard,
    PaySchedule,
    PmsDailyFinancialStage,
)
from usali.schemas import StagedRecord  # noqa: E402
from usali.stage import stage_records  # noqa: E402
from usali.tenancy import (  # noqa: E402
    MissingOrgContext,
    bind_org_context,
    instrument_org_wall,
)
from usali.workforce import assignment_scope, resolve_scope  # noqa: E402


# =========================================================== F1 (own container)


def _cfg(url: str) -> Config:
    os.environ["USALI_DB_URL"] = url
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _employee_has_org_id(engine) -> bool:
    with engine.begin() as c:
        return c.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='employee' AND column_name='org_id'"
        )).scalar_one() == 1


def _revision(engine) -> str:
    with engine.begin() as c:
        return c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_f1_l1_downgrade_refuses_a_multi_org_db_atomically():
    """A two-org database REFUSES the downgrade past L1 (the symmetric twin of
    the upgrade's single-org guard). Because the whole downgrade command runs
    in one transaction, the refusal is ATOMIC: the revision is unmoved and
    org_id (the tenant discriminator) survives on every table — the
    irreversible commingling the lens demonstrated cannot happen. A single-org
    database still downgrades past L1 cleanly."""
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            ensure_app_role(url)
            ensure_provisioner_role(url)   # b1a0provrole refuses without it too
            cfg = _cfg(url)
            command.upgrade(cfg, "head")
            engine = create_engine(url)

            # --- single-org: downgrade past L1 still works cleanly ----------
            with engine.begin() as c:
                c.execute(text("INSERT INTO organization (org_id, name) VALUES (1,'One')"))
            command.downgrade(cfg, "j3a0crmref")
            assert not _employee_has_org_id(engine), "single-org downgrade must proceed"
            assert _revision(engine) == "j3a0crmref"
            command.upgrade(cfg, "head")  # back to head for the multi-org leg

            # --- multi-org: the downgrade REFUSES, atomically ---------------
            with engine.begin() as c:
                c.execute(text("INSERT INTO organization (org_id, name) VALUES (2,'Two')"))
                c.execute(text(
                    "INSERT INTO property (property_id, org_id, name, pms_source) "
                    "VALUES ('ONE1',1,'One','opera')"))
                c.execute(text(
                    "INSERT INTO property (property_id, org_id, name, pms_source) "
                    "VALUES ('TWO1',2,'Two','opera')"))
            head = _revision(engine)

            with pytest.raises(Exception, match="multi-org"):
                command.downgrade(cfg, "j3a0crmref")

            # Atomic refusal: nothing torn down, tenant attribution intact.
            assert _revision(engine) == head, "the refused downgrade must not move the revision"
            assert _employee_has_org_id(engine), "org_id must survive the refusal"
            with engine.begin() as c:
                orgs = c.execute(text("SELECT count(*) FROM organization")).scalar_one()
            assert orgs == 2, "both tenants still distinguishable by org_id"
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous


# ============================================================ F2 (shared world)


def test_f2_pay_schedule_cross_org_squat_is_refused(two_tenant_world, app_role_engine):
    """An org-2 session that names ORG 1's property ONE1 in a pay_schedule is
    REFUSED at the composite FK — (org_id=2, property_id='ONE1') is not a
    property row, so the one-per-property squat is structurally impossible.
    Control: the SAME shape naming org 2's OWN property lands."""
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        s.add(PaySchedule(property_id="ONE1", anchor=date(2026, 1, 5)))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()

    # Control: in-org reference succeeds (the FK is not a blanket refusal).
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        s.add(PaySchedule(property_id="TWO1", anchor=date(2026, 1, 5)))
        s.commit()
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        assert set(s.execute(select(PaySchedule.property_id)).scalars()) == {"TWO1"}

    # Org 1 is NOT blocked: it can create its own pay_schedule for ONE1.
    with factory() as s:
        bind_org_context(s, 1)
        s.add(PaySchedule(property_id="ONE1", anchor=date(2026, 1, 5)))
        s.commit()
        assert set(s.execute(select(PaySchedule.property_id)).scalars()) == {"ONE1"}


def test_f2_labor_standard_cross_org_squat_is_refused(two_tenant_world, app_role_engine):
    """The re-audit twin: labor_standard's one-per-department global unique.
    An org-2 session naming ORG 1's department is refused at the composite FK.
    Control: org 2 may create a labor_standard for its OWN department."""
    factory = make_session_factory(app_role_engine)
    # Give each org a department; capture org 1's department_id.
    with factory() as s:
        bind_org_context(s, 1)
        d1 = Department(property_id="ONE1", name="Housekeeping")
        s.add(d1)
        s.commit()
        org1_dept = d1.department_id
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        d2 = Department(property_id="TWO1", name="Housekeeping")
        s.add(d2)
        s.commit()
        org2_dept = d2.department_id

    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        s.add(LaborStandard(property_id="TWO1", department_id=org1_dept,
                            basis="fixed_hours_per_day", value=8))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()

    # Control: org 2's own department is fine.
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        s.add(LaborStandard(property_id="TWO1", department_id=org2_dept,
                            basis="fixed_hours_per_day", value=8))
        s.commit()
        assert s.execute(
            select(func.count()).select_from(LaborStandard)
        ).scalar_one() == 1


# ============================================================ F3 (shared world)


def _fin_rec(prop: str) -> StagedRecord:
    return StagedRecord(
        property_id=prop, pms_source="opera", report_type="financial",
        business_date=date(2026, 7, 7), pms_trx_code="1000",
        raw_amount=Decimal("100.00"),
    )


def test_f3_stage_records_stamps_the_bound_org(two_tenant_world, app_role_engine):
    """The REAL ingestion path on an org-2-bound app-role session: the Core
    insert now stamps org_id from the session, so the rows land in org 2 (not a
    raw RLS 500, not the server default '1'). They read back under org 2's wall
    and are ABSENT under org 1's."""
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        stage_records(s, [_fin_rec("TWO1")], source_file="two.pdf", file_hash="f3abc")
        s.commit()

    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        rows = s.execute(select(PmsDailyFinancialStage)).scalars().all()
        assert {r.property_id for r in rows} == {"TWO1"}
        assert all(r.org_id == two_tenant_world.org2_id for r in rows)
    with factory() as s:
        bind_org_context(s, 1)
        assert s.execute(
            select(func.count()).select_from(PmsDailyFinancialStage)
        ).scalar_one() == 0


def test_f3_contextless_session_refuses_loudly(two_tenant_world, app_role_engine):
    """A session that is org-instrumented but never bound refuses LOUDLY
    (MissingOrgContext) rather than silently filing the rows into org 1 — the
    write-side mirror of the read wall's refusal."""
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        instrument_org_wall(s)  # walls attached, no org bound
        with pytest.raises(MissingOrgContext):
            stage_records(s, [_fin_rec("TWO1")], source_file="x.pdf", file_hash="none")


# ============================================================ F4 (shared world)


def test_f4_cross_org_row_hash_collision_keeps_both(two_tenant_world, app_role_engine):
    """Two orgs stage rows whose CONTENT hashes identically (row_hash omits
    property_id/org). With org_id now in the stage unique — and in the stagers'
    on_conflict target — each org keeps its OWN row; the second is no longer
    silently dropped across the tenant wall."""
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, 1)
        stage_records(s, [_fin_rec("ONE1")], source_file="dup.pdf", file_hash="collide")
        s.commit()
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        stage_records(s, [_fin_rec("TWO1")], source_file="dup.pdf", file_hash="collide")
        s.commit()

    with factory() as s:
        bind_org_context(s, 1)
        one = s.execute(select(PmsDailyFinancialStage)).scalars().all()
        assert {r.property_id for r in one} == {"ONE1"}, "org 1 keeps its row"
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        two = s.execute(select(PmsDailyFinancialStage)).scalars().all()
        assert {r.property_id for r in two} == {"TWO1"}, "org 2's colliding row survives"


# ============================================================ F5 (shared world)


def test_f5_scopes_claim_naming_a_foreign_org_property_is_fenced_out(
    two_tenant_world, app_role_engine
):
    """resolve_scope intersects the realm-global scopes claim with the
    properties visible under the active org. A claim naming ORG 1's property
    ONE1, resolved while active in org 2, grants NO access to it — property_id
    is globally unique, but the gate can no longer name a property outside the
    active org. The in-org claim still works."""
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        foreign = Principal(subject="op", username="op",
                            roles=frozenset({"property_gm"}),
                            scopes=frozenset({("ONE1", None)}))
        scope = resolve_scope(foreign, s)
        assert not scope.allows_property("ONE1"), "foreign-org property fenced out"
        assert scope.properties == frozenset()

        in_org = Principal(subject="op", username="op",
                           roles=frozenset({"property_gm"}),
                           scopes=frozenset({("TWO1", None)}))
        scope2 = resolve_scope(in_org, s)
        assert scope2.allows_property("TWO1"), "the in-org scopes-claim path still works"
        assert not scope2.allows_property("ONE1")


def test_f5_assignment_scope_org_fence_is_pinned_independently(
    two_tenant_world, app_role_engine
):
    """assignment_scope — the WRITE-confinement path (onboarding authority),
    the more security-sensitive of the two claim callers — carries the SAME
    org-fence as resolve_scope through the shared _scope_from_claim. Pinned
    independently so a future edit that diverges the two callers cannot leave
    the write path unfenced: a claim naming ORG 1's property, resolved while
    active in org 2, yields an assignment_scope that does NOT include it, while
    the in-org claim path still resolves."""
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, two_tenant_world.org2_id)
        foreign = Principal(subject="op", username="op",
                            roles=frozenset({"property_gm"}),
                            scopes=frozenset({("ONE1", None)}))
        scope = assignment_scope(foreign, s)
        assert not scope.allows_property("ONE1"), "foreign-org property fenced out"
        assert scope.properties == frozenset()

        in_org = Principal(subject="op", username="op",
                           roles=frozenset({"property_gm"}),
                           scopes=frozenset({("TWO1", None)}))
        scope2 = assignment_scope(in_org, s)
        assert scope2.allows_property("TWO1"), "the in-org claim path still resolves"
        assert not scope2.allows_property("ONE1")
