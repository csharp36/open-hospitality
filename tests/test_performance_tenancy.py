"""App-role cross-org tenancy for the performance surfaces (#9 review).

Mirrors tests/test_property_config_tenancy.py: the full RLS-bound app-role
serving stack (`rls_client`) over the shared two-org world, NOT the superuser
`db_engine` the other performance tests use. Org 1 owns property ONE1 with a
COMPLETE performance dataset — inventory, statistics, a room-revenue financial
fact, a stat-config and an ingestion-coverage row — so every one of these
endpoints WOULD return org-1 figures if the wall failed. An org-2 admin, active
in org 2, must instead be told "nothing here":

  * org_admin passes the SCOPE gate (an org-wide grant is all-properties), so
    the refusal is not a 403 — RLS empties org 1's rows and the endpoint's
    fail-loud `InventoryNotConfigured` surfaces as 409, with NO org-1 numbers in
    the body.
  * the room-revenue drill-through returns an EMPTY transaction list.
  * a foreign-org property (ONE1) and a NONEXISTENT property (ZZZZ) return the
    SAME 409, so property_ids can't be enumerated through the status code.
  * the two new OrgScoped tables (property_stat_config, ingestion_coverage) are
    RLS-invisible cross-org at the row level.

These are the STANDING RLS-ON guard for the performance tables: each assertion
is designed to FAIL if `FORCE ROW LEVEL SECURITY` or the `org_wall` policy is
dropped from a perf table (an org-2 session would then see org 1's rows and the
endpoints would answer with org-1 figures instead of refusing).
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select

from tests.authkit import make_authkit
from tests.orgworld import ORG2_ALIAS, rls_client
from tests.test_performance_service import _stat
from usali.auth import ACTIVE_ORG_HEADER
from usali.db import make_session_factory
from usali.models import (
    IngestBatch,
    IngestionCoverage,
    PmsDailyFinancialStage,
    PropertyStatConfig,
    RoomInventory,
    UsaliFinancialFact,
)
from usali.tenancy import bind_org_context

# The org-1 property the shared world (tests/orgworld.py) already stands up.
ONE1 = "ONE1"
_DAY = date(2026, 1, 1)

# The org-1 figures that MUST NOT leak into an org-2 response. Formatted the way
# the endpoint serializes them, so a raw-body substring search catches a leak.
_OCCUPANCY = "0.8000"       # 80 occupied / 100 available
_ROOM_REVENUE = "12000"
_TOTAL_REVENUE = "18000"

# The room-revenue drill-through line (portal_api._ROOM_REVENUE_LINE).
_ROOM_REVENUE_LINE = ("Operated Departments", "Rooms", "Room Revenue")


def _room_revenue_fact(db_session, pid, d, amount):
    """A Rooms Room-Revenue financial fact + its stage row for `pid` — the
    drill-through target `line_transactions` reads. Written on the owner session
    so its org_id defaults to the founding org (1) that owns ONE1."""
    batch = IngestBatch(pms_source="opera", report_type="trial_balance",
                        source_file="one1.pdf", file_hash="f" * 64, status="staged",
                        row_count=1, error_count=0)
    db_session.add(batch)
    db_session.flush()
    stage = PmsDailyFinancialStage(
        property_id=pid, pms_source="opera", report_type="trial_balance",
        business_date=d, pms_trx_code="1000", raw_amount=amount,
        source_file="one1.pdf", ingest_batch_id=batch.batch_id, row_hash=f"rr-{pid}-{d}")
    db_session.add(stage)
    db_session.flush()
    major, sub, line = _ROOM_REVENUE_LINE
    db_session.add(UsaliFinancialFact(
        property_id=pid, pms_source="opera", business_date=d, usali_edition=12,
        usali_schedule_id=1, usali_major_category=major, usali_sub_category=sub,
        usali_line_item=line, amount=amount, ingest_batch_id=batch.batch_id,
        stage_id=stage.stage_id))
    db_session.flush()


def _seed_org1_performance_world(db_session):
    """Give org-1's ONE1 a complete performance dataset on the OWNER session
    (org_id defaults to 1). Everything an org-2 caller would see IF the wall
    failed."""
    db_session.add(RoomInventory(property_id=ONE1, effective_date=_DAY, total_rooms=100))
    db_session.add_all([
        _stat(db_session, ONE1, _DAY, "ROOMS_OCCUPIED", 80),
        _stat(db_session, ONE1, _DAY, "ROOM_REVENUE", 12000),
        _stat(db_session, ONE1, _DAY, "TOTAL_REVENUE", 18000),
    ])
    _room_revenue_fact(db_session, ONE1, _DAY, Decimal("12000"))
    db_session.add(PropertyStatConfig(property_id=ONE1, adr_room_basis="as_reported"))
    db_session.add(IngestionCoverage(property_id=ONE1, business_date=_DAY,
                                     report_type="manager_flash"))
    db_session.commit()


def _org2_admin_headers(world, mint):
    tok = mint(roles=["org_admin"], sub=world.org2_admin, organizations=[ORG2_ALIAS])
    return {"Authorization": f"Bearer {tok}", ACTIVE_ORG_HEADER: ORG2_ALIAS}


def test_org2_admin_gets_409_not_org1_performance_numbers(
    db_url, db_session, two_tenant_world, tmp_path
):
    """The core RLS-ON pin: an org-2 admin, active in org 2, requests org-1's
    ONE1 performance. The scope gate lets an org-wide admin through (so this is
    NOT a 403), but org 2's RLS empties ONE1's inventory + statistics, so the
    endpoint refuses with 409 and NO org-1 figure appears in the body."""
    world = two_tenant_world
    _seed_org1_performance_world(db_session)
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    headers = _org2_admin_headers(world, mint)

    r = client.get(f"/api/performance?property={ONE1}&from={_DAY}&to={_DAY}", headers=headers)
    assert r.status_code == 409, r.text
    body = r.text
    for leaked in (_OCCUPANCY, _ROOM_REVENUE, _TOTAL_REVENUE):
        assert leaked not in body, f"org-1 figure {leaked!r} leaked cross-org: {body}"


def test_org2_admin_drill_through_sees_no_org1_transactions(
    db_url, db_session, two_tenant_world, tmp_path
):
    """The room-revenue drill-through: org 1's Room-Revenue financial fact is
    RLS-invisible to org 2, so the transaction list comes back EMPTY. A dropped
    perf-table policy would surface org 1's transaction here."""
    world = two_tenant_world
    _seed_org1_performance_world(db_session)
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    headers = _org2_admin_headers(world, mint)

    r = client.get(
        f"/api/performance/room-revenue/transactions?property={ONE1}&from={_DAY}&to={_DAY}",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["transactions"] == []
    assert _ROOM_REVENUE not in r.text


def test_foreign_org_and_nonexistent_property_are_indistinguishable(
    db_url, db_session, two_tenant_world, tmp_path
):
    """Existence oracle (#9 review): a FOREIGN-org property (ONE1, emptied by
    RLS) and a NONEXISTENT property (ZZZZ) must return the SAME status — both
    409 under the RLS-empty inventory — so an org-2 admin cannot enumerate other
    orgs' property_ids by watching for a status difference (a 404/409 split, or a
    403 on one and 409 on the other, would leak which ids exist)."""
    world = two_tenant_world
    _seed_org1_performance_world(db_session)
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    headers = _org2_admin_headers(world, mint)

    foreign = client.get(f"/api/performance?property={ONE1}&from={_DAY}&to={_DAY}", headers=headers)
    absent = client.get(f"/api/performance?property=ZZZZ&from={_DAY}&to={_DAY}", headers=headers)
    assert foreign.status_code == absent.status_code == 409


def test_new_perf_tables_are_rls_invisible_cross_org(
    db_url, db_session, two_tenant_world, app_role_engine
):
    """The two new OrgScoped tables at the ROW level: org 1's PropertyStatConfig
    and IngestionCoverage rows for ONE1 are visible to an org-1-bound app-role
    session but INVISIBLE to an org-2-bound one. The positive (org-1) leg proves
    the rows exist, so the org-2 zero is the wall working — not an empty seed."""
    _seed_org1_performance_world(db_session)
    factory = make_session_factory(app_role_engine)

    def _counts(org_id):
        with factory() as s:
            bind_org_context(s, org_id)
            cfg = s.execute(
                select(func.count()).select_from(PropertyStatConfig)
                .where(PropertyStatConfig.property_id == ONE1)
            ).scalar_one()
            cov = s.execute(
                select(func.count()).select_from(IngestionCoverage)
                .where(IngestionCoverage.property_id == ONE1)
            ).scalar_one()
        return cfg, cov

    # Org 1 (the owner) sees its rows; org 2 sees none of them.
    assert _counts(1) == (1, 1)
    assert _counts(2) == (0, 0)
