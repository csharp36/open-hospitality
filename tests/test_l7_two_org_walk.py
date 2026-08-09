"""L7 (Pillar L): the two-org isolation walk — one full story, end to end.

This is the pillar's demonstrable proof: two fictitious tenants stood up side
by side, fully isolated through the REAL walls. Unlike the per-decision pins
(L2 RLS, L3 resolution, L4 grants, L5 stores, L6 provisioning), this walk
exercises them TOGETHER on one world:

  - ORG 1 is the founding demo world (alias `pilot-hotel-group`) — a property,
    an employee, `crm_provider=delphi`, an org-wide `org_admin` grant. Every
    existing demo talking point stays org 1's; the walk only ADDS org 2 beside
    it (the live cloud demo remains a single fictitious org — this world is a
    TEST, never the default seed).
  - ORG 2 is a SECOND fictitious hotel group, stood up by `provision_tenant()`
    (L6b) on the owner session, then seeded a minimal world (a property + an
    employee) through an ORG-2-BOUND app-role session — which also proves the
    L6a write path lets a provisioned tenant WRITE through the real RLS
    `WITH CHECK` (the prerequisite that made org != 1 worlds writable at all).

The world itself lives in `tests/orgworld.py` (the `two_tenant_world` and
`app_role_engine` fixtures come from `conftest.py`), shared with the L8
adversarial lenses so they probe the SAME world this walk proved clean. The
isolation assertions run on the RLS-bound `usali_app` role (NOT the superuser
engine), so the database wall is genuine — a dropped policy or a widened grant
read would surface here, not be bypassed. Both orgs are fictitious-by-
construction, the same posture the demo roster holds, now proven PER-ORG.
"""

from sqlalchemy import func, select

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.orgworld import ORG1_ADMIN, ORG2_ALIAS, rls_client
from usali.auth import ACTIVE_ORG_HEADER, effective_roles
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import Employee, Organization, OrgSettings, Property, RoleAssignment
from usali.photo_store import InMemoryPhotoStore, face_reference_key
from usali.provisioning import provision_tenant
from usali.tenancy import bind_org_context


def test_two_tenants_coexist_fully_isolated(two_tenant_world, app_role_engine):
    """THE walk: on the RLS-bound app role, each org's admin sees ONLY its own
    tenant — properties, employees, grants, and per-org settings — and none of
    the other's. One world, both walls, all five decisions at once."""
    w = two_tenant_world
    factory = make_session_factory(app_role_engine)

    # ---- Active in ORG 1: org 1's data, NONE of org 2's -------------------
    with factory() as s:
        bind_org_context(s, 1)
        assert set(s.execute(select(Property.property_id)).scalars()) == {"ONE1"}
        assert set(s.execute(select(Employee.full_name)).scalars()) == {"Ada One"}
        # Grants: org 1's admin has authority here; org 2's admin does NOT —
        # its grant lives in org 2 and org 1's walls show none of it.
        assert effective_roles(s, ORG1_ADMIN) == frozenset({"org_admin"})
        assert effective_roles(s, w.org2_admin) == frozenset()
        # Per-org settings (L5): org 1 runs delphi; org 2's row is invisible,
        # so exactly one org_settings row is in view.
        assert s.get(OrgSettings, 1).crm_provider == "delphi"
        assert s.execute(
            select(func.count()).select_from(OrgSettings)
        ).scalar_one() == 1

    # ---- Active in ORG 2: org 2's data, NONE of org 1's -------------------
    with factory() as s:
        bind_org_context(s, w.org2_id)
        assert set(s.execute(select(Property.property_id)).scalars()) == {"TWO1"}
        assert set(s.execute(select(Employee.full_name)).scalars()) == {"Cy Two"}
        assert effective_roles(s, w.org2_admin) == frozenset({"org_admin"})
        assert effective_roles(s, ORG1_ADMIN) == frozenset()
        # Org 2 was provisioned, never given an org_settings row: crm is OFF,
        # and org 1's delphi row is not visible here either.
        assert s.execute(
            select(func.count()).select_from(OrgSettings)
        ).scalar_one() == 0

    # ---- The store outside Postgres (L5 photos): KEY NAMESPACING only. This
    # leg asserts that `face_reference_key(org, emp)` separates the SAME
    # employee id into two org-prefixed slots, so no cross-org overwrite — NOT
    # end-to-end store-crypto isolation (that an org cannot decrypt another's
    # ciphertext even handed the bytes is pinned in test_l5_per_org_stores).
    store = InMemoryPhotoStore()
    store.put(face_reference_key(1, w.org2_emp_id), b"\xff\xd8\xffONE")
    store.put(face_reference_key(w.org2_id, w.org2_emp_id), b"\xff\xd8\xffTWO")
    assert store.get(face_reference_key(1, w.org2_emp_id)) == b"\xff\xd8\xffONE"
    assert store.get(face_reference_key(w.org2_id, w.org2_emp_id)) == b"\xff\xd8\xffTWO"


def test_marquee_org1_admin_active_in_org2_is_refused(
    two_tenant_world, db_url, tmp_path
):
    """The marquee cross-tenant refusal through the FULL stack: org 1's admin
    is a member of both orgs and carries the org_admin realm role, but active
    in org 2 every org-gated surface refuses (403) — the token's realm role
    decides nothing; only a grant visible under org 2's walls would, and there
    is none. The control arm (same token, active in org 1) opens the gate,
    proving the refusal is the wall, not a world that refuses everyone."""
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    token = mint(roles=["org_admin"], sub=ORG1_ADMIN,
                 organizations=[DEFAULT_ORG_ALIAS, ORG2_ALIAS])

    refused = client.get("/api/kiosk-devices", headers={
        "Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: ORG2_ALIAS,
    })
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "insufficient role"

    allowed = client.get("/api/kiosk-devices", headers={
        "Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: DEFAULT_ORG_ALIAS,
    })
    assert allowed.status_code == 200, allowed.text


def test_provisioned_org2_admin_acts_only_in_its_own_org(
    two_tenant_world, db_url, tmp_path
):
    """The other side of the marquee: the PROVISIONED org 2 admin, active in
    org 2, holds org_admin authority there and can WRITE (create an employee at
    org 2's property) — the whole chain end to end (KC org -> membership claim
    -> alias resolved to org_id -> grant read -> write-stamp -> RLS WITH CHECK).
    The same admin, active in org 1 (of which the token also claims membership),
    is refused: its grant does not cross the wall."""
    verifier, mint = make_authkit()
    client = rls_client(db_url, tmp_path, verifier)
    w = two_tenant_world
    token = mint(roles=["org_admin"], sub=w.org2_admin,
                 organizations=[ORG2_ALIAS, DEFAULT_ORG_ALIAS])

    created = client.post("/api/employees", json={
        "full_name": "New Two Hire", "property": "TWO1", "pay_type": "hourly",
    }, headers={
        "Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: ORG2_ALIAS,
    })
    assert created.status_code == 201, created.text

    refused = client.get("/api/kiosk-devices", headers={
        "Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: DEFAULT_ORG_ALIAS,
    })
    assert refused.status_code == 403, refused.text


def test_standing_up_the_two_org_world_is_idempotent(
    db_session, founding_org, app_role_engine
):
    """Seed idempotence, at the whole-world level: re-provisioning org 2 and
    re-writing its admin grant changes nothing — one KC org, one DB org row,
    one grant (find-or-create at every step, DB-guarded by the NULLS-NOT-
    DISTINCT unique). The demo/seed posture, proven for the provisioning path
    the two-org world stands on."""
    kc = InMemoryKeycloakAdmin()
    first = provision_tenant(
        db_session, kc,
        org_name="Second Hotel Group", org_alias=ORG2_ALIAS,
        admin_username="org2-admin", admin_email="admin@second.example",
        admin_full_name="Bo Two",
    )
    db_session.commit()
    second = provision_tenant(
        db_session, kc,
        org_name="Second Hotel Group", org_alias=ORG2_ALIAS,
        admin_username="org2-admin", admin_email="admin@second.example",
        admin_full_name="Bo Two",
    )
    db_session.commit()

    assert second == first  # same ids, same subject
    assert len(kc.organizations) == 1 and len(kc.users) == 1
    assert db_session.execute(
        select(func.count()).select_from(Organization)
        .where(Organization.kc_org_alias == ORG2_ALIAS)
    ).scalar_one() == 1
    assert db_session.execute(
        select(func.count()).select_from(RoleAssignment)
        .where(RoleAssignment.keycloak_subject == first.admin_subject)
    ).scalar_one() == 1
