"""L4 (Pillar L decision 4): role authority = org-scoped DB grants.

Realm token roles are realm-GLOBAL — an org_admin of tenant A carries
the role in a token used while active in tenant B — so token roles gate
only the coarse operator/employee door. Org authority is the
`role_assignment` grants VISIBLE UNDER THE ACTIVE ORG'S RLS:
`property_id` NULL = org-wide grant (the department_id-NULL precedent,
one level up).

The marquee pin, and the mutants it kills in one world: the subject
holds org-wide org_admin + payroll_admin grants in org A ONLY, is a
member of BOTH orgs, and is refused on every org-gated surface while
active in org B — while the SAME token succeeds in org A.

- *Grant intersection dropped (token roles honored alone)*: the token
  carries org_admin/payroll_admin realm roles; honoring them without
  the DB read turns every B-side 403 into a success.
- *RLS-scoped grant read widened*: the grant rows EXIST — in org A. A
  grant read that escapes the active org's walls (e.g. through the
  base, un-instrumented session factory) finds them from B and turns
  the same 403s into successes. The org-bound request session IS the
  mechanism; there is no separate per-query org filter to drop.

Also pinned here: the l4a0orggrant schema judgments (NULLS NOT
DISTINCT — one grant one row; the MATCH SIMPLE composite FK tolerating
NULL property_id), the exact alembic head, the seed's persona grants
(idempotent; the dropped-seed-grant mutant), and the coarse gate
holding against a stale grant row.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from tests.orgwall import ensure_app_role, ensure_provisioner_role

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from usali.auth import ACTIVE_ORG_HEADER
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.mapping.property_registry import ensure_default_org
from usali.models import Organization, Property, RoleAssignment
from usali.opener import SoftwareOpener
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app

RIVAL_ALIAS = "rival-hotel-group"
ADMIN_SUB = "admin-of-a"


@pytest.fixture
def two_org_world(db_session):
    """Org 1 (founding, seeded as production seeds it) and org 2, one
    property each; ADMIN_SUB holds org-wide org_admin + payroll_admin
    grants IN ORG 1 ONLY, plus one org-1 employee to act on. Written by
    the superuser session so the world exists regardless of the walls."""
    ensure_default_org(db_session)
    db_session.add(Organization(
        org_id=2, name="Rival Hotel Group", kc_org_alias=RIVAL_ALIAS
    ))
    db_session.flush()
    db_session.add(Property(
        property_id="L4A1", org_id=1, name="Org A Hotel", pms_source="opera"
    ))
    db_session.add(Property(
        property_id="L4B1", org_id=2, name="Org B Hotel", pms_source="opera"
    ))
    db_session.flush()
    employee = make_employee(
        db_session, property_id="L4A1",
        full_name="Org A Person", pay_type="hourly",
    )
    db_session.commit()
    grant_role(db_session, "org_admin", sub=ADMIN_SUB, org_id=1)
    grant_role(db_session, "payroll_admin", sub=ADMIN_SUB, org_id=1)
    return employee.employee_id


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
        opener=SoftwareOpener.generate(key_id="l4"),
    )
    return TestClient(app)


def _mint_admin_of_a(mint):
    """org_admin/payroll_admin REALM roles (the coarse gate passes), a
    member of both orgs — but grants only in org A (the fixture)."""
    return mint(
        roles=["org_admin", "payroll_admin"], sub=ADMIN_SUB,
        organizations=[DEFAULT_ORG_ALIAS, RIVAL_ALIAS],
    )


def _headers(token, alias):
    return {"Authorization": f"Bearer {token}", ACTIVE_ORG_HEADER: alias}


# The org-gated battery: one surface per shifted gate family. Bodies are
# valid so a refusal can only be the gate.
def _org_gated_calls(client, headers, employee_id):
    return [
        ("POST /api/employees", client.post(
            "/api/employees",
            json={"full_name": "New Hire", "property": "L4A1",
                  "pay_type": "hourly"},
            headers=headers,
        )),
        ("PATCH /api/employees/{id}", client.patch(
            f"/api/employees/{employee_id}",
            json={"employment_status": "inactive"}, headers=headers,
        )),
        ("GET compensation", client.get(
            f"/api/employees/{employee_id}/compensation", headers=headers,
        )),
        ("PUT pay-rate", client.put(
            f"/api/employees/{employee_id}/pay-rate",
            json={"pay_rate": "20.00"}, headers=headers,
        )),
        ("POST kiosk-devices", client.post(
            "/api/kiosk-devices",
            json={"property": "L4A1", "name": "Lobby"}, headers=headers,
        )),
        ("GET kiosk-devices", client.get(
            "/api/kiosk-devices", headers=headers,
        )),
        ("GET timecards", client.get(
            "/api/timecards", headers=headers,
        )),
        ("GET schedule templates", client.get(
            "/api/schedule/templates?property=L4A1", headers=headers,
        )),
        ("POST crm refresh", client.post(
            "/api/crm/refresh", json={"property": "L4A1"}, headers=headers,
        )),
        ("GET payroll runs", client.get(
            "/api/payroll/runs?property=L4A1", headers=headers,
        )),
        ("GET pii profile", client.get(
            f"/api/payroll/employees/{employee_id}/profile", headers=headers,
        )),
        ("GET sick balance", client.get(
            f"/api/payroll/employees/{employee_id}/sick-leave",
            headers=headers,
        )),
    ]


def test_marquee_org_admin_of_a_active_in_b_refused_everywhere(
    db_engine, tmp_path, two_org_world
):
    """THE decision-4 pin: grants live in org A; active in org B every
    org-gated surface refuses 403 — the token's realm roles (which
    still say org_admin) must decide nothing."""
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    token = _mint_admin_of_a(mint)
    for name, response in _org_gated_calls(
        client, _headers(token, RIVAL_ALIAS), two_org_world
    ):
        assert response.status_code == 403, (
            f"{name}: expected 403 in org B, got "
            f"{response.status_code}: {response.text}"
        )
        assert response.json()["detail"] == "insufficient role"


def test_the_same_token_has_authority_in_its_own_org(
    db_engine, tmp_path, two_org_world
):
    """The marquee's control arm: identical token, active in org A, and
    the grant-backed gates OPEN — proof the refusals in B come from the
    org walls, not from a world that would refuse everyone everywhere."""
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    token = _mint_admin_of_a(mint)
    headers = _headers(token, DEFAULT_ORG_ALIAS)
    r = client.get("/api/kiosk-devices", headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(
        f"/api/employees/{two_org_world}/compensation", headers=headers
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/employees",
        json={"full_name": "New Hire", "property": "L4A1",
              "pay_type": "hourly"},
        headers=headers,
    )
    assert r.status_code == 201, r.text


def test_org_wide_grant_grants_org_wide(db_engine, tmp_path, db_session):
    """A property_id-NULL grant reaches EVERY property of its org — the
    all-properties short-circuit now comes from the grant row, not the
    token role."""
    ensure_default_org(db_session)
    db_session.add(Property(
        property_id="L4A1", org_id=1, name="One", pms_source="opera"
    ))
    db_session.add(Property(
        property_id="L4A2", org_id=1, name="Two", pms_source="opera"
    ))
    db_session.commit()
    grant_role(db_session, "accountant")  # org-wide, sub=user-1
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["accountant"])
    for prop in ("L4A1", "L4A2"):
        r = client.get(
            f"/api/sos?property={prop}&date=2026-07-07",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code != 403, r.text


def test_property_grant_still_confines(db_engine, tmp_path, db_session):
    """A property-scoped grant is NOT an org-wide one: the GM reaches
    their property and is refused on the neighbor — through the DB path
    (no scopes claim minted), the post-L4 production shape."""
    ensure_default_org(db_session)
    db_session.add(Property(
        property_id="L4A1", org_id=1, name="One", pms_source="opera"
    ))
    db_session.add(Property(
        property_id="L4A2", org_id=1, name="Two", pms_source="opera"
    ))
    db_session.commit()
    grant_role(db_session, "property_gm", property_id="L4A1")
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["property_gm"], organizations=[DEFAULT_ORG_ALIAS])
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(
        "/api/sos?property=L4A1&date=2026-07-07", headers=headers
    ).status_code != 403
    assert client.get(
        "/api/sos?property=L4A2&date=2026-07-07", headers=headers
    ).status_code == 403
    # And the ROLE gate passes on grant, confinement on scope: the GM
    # may enroll a kiosk at their own property, not the neighbor.
    assert client.post(
        "/api/kiosk-devices", json={"property": "L4A2", "name": "X"},
        headers=headers,
    ).status_code == 403


def test_the_coarse_employee_gate_still_holds(
    db_engine, tmp_path, db_session
):
    """An `employee` realm token is refused at the operator door even
    when a (stale/mistaken) org_admin grant row exists for the subject:
    the DB grant must not out-rank the realm's coarse claim — the two
    factors INTERSECT."""
    ensure_default_org(db_session)
    db_session.commit()
    grant_role(db_session, "org_admin")  # sub=user-1, org-wide
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["employee"])
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


# ------------------------------------------------------------ schema pins


def test_duplicate_org_wide_grant_is_refused_by_the_database(db_session):
    """The open-question pin, decided: NULLS NOT DISTINCT. A grant is
    now THE authority and revocation is a row delete — a duplicate
    grant would survive its own revocation, so the database refuses the
    twin rather than trusting every writer to find-or-create."""
    ensure_default_org(db_session)
    db_session.commit()
    db_session.add(RoleAssignment(
        keycloak_subject="dup", role="org_admin", property_id=None
    ))
    db_session.commit()
    db_session.add(RoleAssignment(
        keycloak_subject="dup", role="org_admin", property_id=None
    ))
    with pytest.raises(IntegrityError, match="uq_role_assignment_grant"):
        db_session.commit()
    db_session.rollback()


def test_same_org_wide_grant_in_two_orgs_is_two_legal_rows(db_session):
    """Why org_id joined the unique: the SAME subject may hold the SAME
    role org-wide in two orgs — two rows differing only in org_id."""
    ensure_default_org(db_session)
    db_session.add(Organization(
        org_id=2, name="Rival", kc_org_alias=RIVAL_ALIAS
    ))
    db_session.commit()
    for org_id in (1, 2):
        db_session.add(RoleAssignment(
            org_id=org_id, keycloak_subject="both", role="org_admin",
            property_id=None,
        ))
    db_session.commit()
    rows = db_session.execute(
        select(RoleAssignment.org_id).where(
            RoleAssignment.keycloak_subject == "both"
        )
    ).scalars().all()
    assert sorted(rows) == [1, 2]


def test_org_wide_row_passes_the_composite_property_fk(db_session):
    """The MATCH SIMPLE pin: fk_role_assignment_property_org is a
    composite (org_id, property_id) FK, and a NULL property_id skips
    its enforcement entirely — deliberate (an org-wide grant names no
    property), while org_id itself stays enforced by the mixin FK onto
    organization. The insert above every other test in this module
    already relies on this; this pin makes the reliance a stated one —
    and proves the org FK still bites."""
    ensure_default_org(db_session)
    db_session.commit()
    # No property rows exist AT ALL: nothing for (org_id, NULL) to match.
    db_session.add(RoleAssignment(
        keycloak_subject="fkpin", role="org_admin", property_id=None
    ))
    db_session.commit()  # MATCH SIMPLE: unenforced, as designed
    db_session.add(RoleAssignment(
        org_id=77, keycloak_subject="fkpin", role="org_admin",
        property_id=None,
    ))
    with pytest.raises(IntegrityError):  # no org 77: the org FK holds
        db_session.commit()
    db_session.rollback()


def test_l4_is_the_single_alembic_head():
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    # The invariant is ONE head — no branch — not any particular name. L8 built
    # linearly on L5 and L9 (l9a0deptfk) builds linearly on L8, so the name
    # moves each time the chain grows and only the count is the assertion.
    assert ScriptDirectory.from_config(cfg).get_heads() == ["b1c0otp"]


def _mig_cfg(url: str) -> Config:
    # env.py reads USALI_DB_URL (the test_migration_on_populated_data
    # convention; the caller restores the previous value).
    os.environ["USALI_DB_URL"] = url
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_l4_round_trips_with_data_present():
    """The full migration story in one container: pre-L4 duplicate
    grants (legal under the NULLs-distinct quad) are DEDUPED on upgrade
    keeping the oldest row; scoped grants ride the downgrade through
    (the I6 lesson); org-wide rows — which the pre-L4 schema cannot
    hold — are deleted by the downgrade rather than stranding it; NOT
    NULL and the original server-named unique come back; and the
    re-upgrade converges."""
    previous = os.environ.get("USALI_DB_URL")
    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            url = pg.get_connection_url()
            ensure_app_role(url)
            ensure_provisioner_role(url)   # b1a0provrole refuses without it too
            cfg = _mig_cfg(url)
            command.upgrade(cfg, "l3a0aliaslookup")
            engine = create_engine(url)

            def unique_names() -> set[str]:
                with engine.begin() as conn:
                    return set(conn.execute(text(
                        "SELECT conname FROM pg_constraint WHERE conrelid "
                        "= 'role_assignment'::regclass AND contype = 'u'"
                    )).scalars())

            old_unique = unique_names()
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO organization (org_id, name) "
                    "VALUES (1, 'Org')"
                ))
                conn.execute(text(
                    "INSERT INTO property (property_id, org_id, name, "
                    "pms_source) VALUES ('P1', 1, 'One', 'opera')"
                ))
                # Twin grants: identical but for assignment_id — the old
                # quad unique admits both (NULL department_id).
                for _ in range(2):
                    conn.execute(text(
                        "INSERT INTO role_assignment (keycloak_subject, "
                        "role, property_id) VALUES ('gm', 'property_gm', "
                        "'P1')"
                    ))
            command.upgrade(cfg, "head")
            assert unique_names() == {"uq_role_assignment_grant"}
            with engine.begin() as conn:
                survivors = conn.execute(text(
                    "SELECT assignment_id FROM role_assignment "
                    "WHERE keycloak_subject = 'gm'"
                )).scalars().all()
                # Exactly the OLDEST of the two twins (serial ids 1, 2
                # on a fresh database) survives the dedupe.
                assert survivors == [1]
                conn.execute(text(
                    "INSERT INTO role_assignment (keycloak_subject, role) "
                    "VALUES ('adm', 'org_admin')"  # org-wide: NULL property
                ))
            command.downgrade(cfg, "l3a0aliaslookup")
            assert unique_names() == old_unique  # original name restored
            with engine.begin() as conn:
                rows = conn.execute(text(
                    "SELECT keycloak_subject, property_id "
                    "FROM role_assignment"
                )).all()
                # The scoped grant rode through; the org-wide row (which
                # this schema cannot hold) is gone.
                assert rows == [("gm", "P1")]
                nullable = conn.execute(text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'role_assignment' "
                    "AND column_name = 'property_id'"
                )).scalar_one()
                assert nullable == "NO"
            command.upgrade(cfg, "head")
            assert unique_names() == {"uq_role_assignment_grant"}
            engine.dispose()
    finally:
        if previous is None:
            os.environ.pop("USALI_DB_URL", None)
        else:
            os.environ["USALI_DB_URL"] = previous


# ------------------------------------------------------------- seed grants


_SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed_l4", _SCRIPT)
assert _spec is not None and _spec.loader is not None
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)


def _kc_with_personas() -> InMemoryKeycloakAdmin:
    kc = InMemoryKeycloakAdmin()
    for username, role in (
        ("dev-gm", "property_gm"), ("dev-admin", "org_admin"),
        ("dev-payroll", "payroll_admin"), ("dev-accountant", "accountant"),
    ):
        kc.create_user(
            username=username, email=f"{username}@example.test",
            full_name=username, realm_roles=[role],
        )
    return kc


def _seed_grant_world(db_session):
    ensure_default_org(db_session)
    for pid in ("HISJ", "SSSJ"):
        db_session.add(Property(
            property_id=pid, org_id=1, name=pid, pms_source="opera"
        ))
    db_session.commit()


def _org_wide_grants(db_session):
    return {
        (subject, role)
        for subject, role in db_session.execute(
            select(
                RoleAssignment.keycloak_subject, RoleAssignment.role
            ).where(RoleAssignment.property_id.is_(None))
        ).all()
    }


def test_seed_writes_the_persona_org_wide_grants(db_session, monkeypatch):
    """The dropped-seed-grant mutant: no SQL backfill can derive grants
    from tokens, so the seed MUST write org-wide rows for dev-admin /
    dev-payroll / dev-accountant (and dev-gm's property rows as before).
    """
    _seed_grant_world(db_session)
    kc = _kc_with_personas()
    monkeypatch.setattr(
        demo_seed.KeycloakAdminClient, "from_settings", lambda settings: kc
    )
    demo_seed._grant_persona_roles(db_session)
    subjects = {
        u["username"]: sid for sid, u in kc.users.items()
    }
    grants = _org_wide_grants(db_session)
    assert (subjects["dev-admin"], "org_admin") in grants
    assert (subjects["dev-payroll"], "payroll_admin") in grants
    assert (subjects["dev-accountant"], "accountant") in grants
    gm_props = set(db_session.execute(
        select(RoleAssignment.property_id).where(
            RoleAssignment.keycloak_subject == subjects["dev-gm"],
            RoleAssignment.role == "property_gm",
        )
    ).scalars())
    assert gm_props == {"HISJ", "SSSJ"}


def test_seed_grants_are_idempotent(db_session, monkeypatch):
    """A re-seed never duplicates or blanks: same rows, same count —
    and since l4a0orggrant a duplicating regression would ALSO explode
    on the NULLS NOT DISTINCT unique rather than pass silently."""
    _seed_grant_world(db_session)
    kc = _kc_with_personas()
    monkeypatch.setattr(
        demo_seed.KeycloakAdminClient, "from_settings", lambda settings: kc
    )
    demo_seed._grant_persona_roles(db_session)
    first = db_session.execute(select(RoleAssignment.assignment_id)).all()
    demo_seed._grant_persona_roles(db_session)
    second = db_session.execute(select(RoleAssignment.assignment_id)).all()
    assert first == second and len(first) == 5  # 3 org-wide + 2 gm rows
