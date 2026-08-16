from datetime import date

from sqlalchemy import func, select, text

from tests.authkit import make_authkit
from tests.orgworld import ORG2_ALIAS, rls_client
from usali.auth import ACTIVE_ORG_HEADER
from usali.models import RoomInventory
from usali.tenancy import RLS_ORG_VAR


def test_tenant_cannot_read_or_write_another_orgs_inventory(
    db_url, db_session, two_tenant_world, tmp_path
):
    """Org 1 owns property ONE1 with an inventory row; an org-2 admin, active in
    org 2, is refused ONE1 entirely (RLS makes it not-there, so the endpoint's
    _require_onboardable_property returns the same 403 as out-of-scope)."""
    world = two_tenant_world
    # Give org 1's property an inventory row (owner session, org 1).
    db_session.add(RoomInventory(property_id="ONE1", org_id=1,
                                 effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.commit()

    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)

    # An org-2 admin token, active org = org 2.
    tok = mint(roles=["org_admin"], sub=world.org2_admin, organizations=[ORG2_ALIAS])
    headers = {"Authorization": f"Bearer {tok}", ACTIVE_ORG_HEADER: ORG2_ALIAS}

    # Read of org 1's property -> 403 (not there under org 2's RLS).
    assert client.get("/api/properties/ONE1/config", headers=headers).status_code == 403
    # Write attempt -> 403, and no row is created anywhere.
    r = client.post("/api/properties/ONE1/inventory",
                    json={"effective_date": "2026-02-01", "total_rooms": 50}, headers=headers)
    assert r.status_code == 403
    db_session.expire_all()
    count = db_session.execute(
        select(func.count()).select_from(RoomInventory).where(RoomInventory.property_id == "ONE1")
    ).scalar_one()
    assert count == 1  # only the original org-1 row; org 2 wrote nothing


def test_org2_admin_writes_land_in_org2(db_url, db_session, two_tenant_world, tmp_path):
    """An org-2 admin, active in org 2, sets inventory on their OWN property
    (TWO1). The write must succeed AND the row must carry org_id=2 — the proof
    the ORM get-or-update path stamps org_id from the bound session, not the
    founding-org column default (1). A Core pg_insert would have mis-stamped or
    been rejected by the RLS WITH CHECK here."""
    world = two_tenant_world
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    tok = mint(roles=["org_admin"], sub=world.org2_admin, organizations=[ORG2_ALIAS])
    headers = {"Authorization": f"Bearer {tok}", ACTIVE_ORG_HEADER: ORG2_ALIAS}

    r = client.post("/api/properties/TWO1/inventory",
                    json={"effective_date": "2026-01-01", "total_rooms": 77}, headers=headers)
    assert r.status_code == 201, r.text
    # Verify via the OWNER session (bypasses RLS) that the row is org 2's.
    db_session.expire_all()
    row = db_session.execute(
        select(RoomInventory).where(RoomInventory.property_id == "TWO1")
    ).scalar_one()
    assert row.org_id == world.org2_id == 2
    assert row.total_rooms == 77


def test_empty_org_context_yields_zero_rows_on_the_new_tables(
    db_session, two_tenant_world, app_role_engine
):
    """The #8 tables copy l2's org_wall predicate INCLUDING the NULLIF(...,'')
    fold. Exercise it directly on the new tables: a pooled app-role connection
    whose app.org_id has reverted to the EMPTY STRING must see zero rows, not
    error on ''::int. test_l2's '' pin only covers property/organization, so
    without this a dropped NULLIF on a #8 table's predicate would survive."""
    db_session.add(RoomInventory(property_id="ONE1", org_id=1,
                                 effective_date=date(2026, 1, 1), total_rooms=100))
    db_session.commit()
    with app_role_engine.connect() as conn:
        try:
            conn.execute(text("SELECT set_config(:v, '', false)"), {"v": RLS_ORG_VAR})
            # These would raise 'invalid input syntax for integer: ""' if the
            # predicate cast '' directly instead of folding it to NULL first.
            assert conn.execute(text("SELECT count(*) FROM room_inventory")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM fiscal_calendar")).scalar() == 0
            assert conn.execute(text("SELECT count(*) FROM out_of_order_room")).scalar() == 0
        finally:
            conn.execute(text(f"RESET {RLS_ORG_VAR}"))
