"""L3 (Pillar L decision 3): membership claim -> validated active org ->
the session variable.

The trust chain, each link pinned here: the token's `organization` claim
(signed by the realm) names the orgs the caller MAY act in, by alias;
`X-Active-Org` picks one; the server refuses anything outside the claim
(403 naming nothing — no existence oracle), refuses ambiguity (400,
never guesses), defaults a single-org token; and the DB alone turns the
validated alias into an org_id (organization.kc_org_alias — nothing
numeric from a token is ever an org_id), which is the ONLY org that
reaches the session binding both L2 walls read.

The one pre-binding read — alias -> org_id under RLS before any org is
bound — goes through the l3a0aliaslookup policy: a second SELECT-only
policy on `organization` keyed on `app.org_alias`. Its pins mirror the
L2 style: unset variable widens nothing, a set variable exposes at most
the one exact-alias row, and the door cannot write.
"""

import importlib.util

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from tests.authkit import make_authkit
from tests.employees import DEFAULT_EFFECTIVE_FROM, make_employee
from tests.grants import grant_role
from tests.orgwall import app_role_url
from usali.auth import ACTIVE_ORG_HEADER, AuthError, _active_org_alias
from usali.db import make_engine, make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.mapping.property_registry import (
    DEFAULT_ORG_ALIAS,
    ensure_default_org,
)
from usali.models import AuditEvent, EmployeeAssignment, Organization, Property
from usali.opener import SoftwareOpener
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.tenancy import (
    FOUNDING_ORG_ID,
    RLS_ALIAS_VAR,
    RLS_ORG_VAR,
    ActiveOrgSessionFactory,
    MissingOrgContext,
    OrgContextMismatch,
    bind_org_context,
    current_org_id,
    instrument_org_wall,
    resolve_org_id,
)

_spec = importlib.util.spec_from_file_location(
    "l3a0aliaslookup",
    "migrations/versions/l3a0aliaslookup_org_alias_lookup_policy.py",
)
assert _spec is not None and _spec.loader is not None
_l3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_l3)

RIVAL_ALIAS = "rival-hotel-group"


def _principal(verifier, mint, **mint_kwargs):
    return verifier.verify(mint(**mint_kwargs))


# ---------------------------------------------------------- claim parsing


def test_org_claim_parses_to_aliases():
    verifier, mint = make_authkit()
    p = _principal(
        verifier, mint, roles=["org_admin"],
        organizations=["pilot-hotel-group", "rival-hotel-group"],
    )
    assert p.org_aliases == frozenset(
        {"pilot-hotel-group", "rival-hotel-group"}
    )


def test_absent_org_claim_is_none_not_empty():
    """KC omits the claim entirely for a user in no organization; absent
    must stay distinguishable from present-and-empty in the Principal
    (both refuse downstream, but the parse must not invent a shape)."""
    verifier, mint = make_authkit()
    assert _principal(
        verifier, mint, roles=["org_admin"], organizations=None
    ).org_aliases is None


@pytest.mark.parametrize(
    "bad",
    [
        {"pilot-hotel-group": {}},  # the mapper's JSON-type=JSON shape: drift
        "pilot-hotel-group",  # the non-multivalued shape: drift
        [1],
        [""],
        [None],
        ["ok", 2],
        42,
    ],
)
def test_malformed_org_claim_refuses(bad):
    """The realm_access discipline: signature trusted, shapes are not —
    any deviation from the pinned array-of-alias-strings is a 401-class
    AuthError, never a TypeError -> 500 and never a silent []."""
    verifier, mint = make_authkit()
    with pytest.raises(AuthError, match="organization"):
        verifier.verify(mint(
            roles=["org_admin"], organizations=None,
            extra_claims={"organization": bad},
        ))


# ------------------------------------------------- active-org selection


def _p(verifier, mint, orgs):
    return _principal(verifier, mint, roles=["org_admin"], organizations=orgs)


def test_single_org_token_defaults_without_header():
    verifier, mint = make_authkit()
    p = _p(verifier, mint, ["pilot-hotel-group"])
    assert _active_org_alias(p, None) == "pilot-hotel-group"


def test_multi_org_token_without_header_is_400_never_a_guess():
    verifier, mint = make_authkit()
    p = _p(verifier, mint, ["a-org", "b-org"])
    with pytest.raises(HTTPException) as exc:
        _active_org_alias(p, None)
    assert exc.value.status_code == 400
    assert ACTIVE_ORG_HEADER in exc.value.detail


def test_non_member_header_is_403_naming_nothing():
    verifier, mint = make_authkit()
    p = _p(verifier, mint, ["a-org"])
    with pytest.raises(HTTPException) as exc:
        _active_org_alias(p, "b-org")
    assert exc.value.status_code == 403
    assert "b-org" not in exc.value.detail
    assert "a-org" not in exc.value.detail


def test_member_header_selects_that_org():
    verifier, mint = make_authkit()
    p = _p(verifier, mint, ["a-org", "b-org"])
    assert _active_org_alias(p, "b-org") == "b-org"


def test_no_membership_refuses_the_founding_org_is_not_a_fallback():
    verifier, mint = make_authkit()
    for orgs in (None, []):
        with pytest.raises(HTTPException) as exc:
            _active_org_alias(_p(verifier, mint, orgs), None)
        assert exc.value.status_code == 403


def test_empty_header_is_treated_as_absent():
    verifier, mint = make_authkit()
    p = _p(verifier, mint, ["a-org"])
    assert _active_org_alias(p, "") == "a-org"


# ------------------------------------------------------------ API worlds


@pytest.fixture
def two_org_world(db_session):
    """Org 1 (the founding org, seeded exactly as production seeds it)
    and org 2 with its own alias — one property and one placed employee
    each, written by the superuser session so the world exists regardless
    of the walls. Org 2's rows carry org_id=2 on every table (employee,
    assignment, property) — the composite-FK world L1 built."""
    ensure_default_org(db_session)
    db_session.add(Organization(
        org_id=2, name="Rival Hotel Group", kc_org_alias=RIVAL_ALIAS
    ))
    db_session.flush()
    db_session.add(Property(
        property_id="L3P1", org_id=1, name="Org One Hotel",
        pms_source="opera",
    ))
    db_session.add(Property(
        property_id="L3P2", org_id=2, name="Org Two Hotel",
        pms_source="opera",
    ))
    db_session.flush()
    make_employee(
        db_session, property_id="L3P1",
        full_name="Org One Person", pay_type="hourly",
    )
    two = make_employee(
        db_session, property_id="L3P2", place_primary=False,
        full_name="Org Two Person", pay_type="hourly", org_id=2,
    )
    db_session.add(EmployeeAssignment(
        org_id=2, employee_id=two.employee_id, property_id="L3P2",
        is_primary=True, status="active",
        effective_from=DEFAULT_EFFECTIVE_FROM,
    ))
    db_session.commit()
    # L4: authority is org-scoped DB grants, not realm roles — the
    # minted org_admin (authkit default sub) holds the grant in BOTH
    # orgs here, because this module tests org RESOLUTION, not grant
    # refusal (test_l4_org_grants owns the grants-in-A-only world).
    grant_role(db_session, "org_admin", org_id=1)
    grant_role(db_session, "org_admin", org_id=2)


def _client(db_engine, tmp_path, verifier):
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
        opener=SoftwareOpener.generate(key_id="l3"),
    )
    return app, TestClient(app)


def _names(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {e["full_name"] for e in response.json()}


def test_single_org_token_reads_its_own_org_by_default(
    db_engine, tmp_path, two_org_world
):
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=[DEFAULT_ORG_ALIAS])
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert _names(r) == {"Org One Person"}


def test_the_default_is_the_tokens_org_not_the_founding_org(
    db_engine, tmp_path, two_org_world
):
    """Kills the wrong-default mutant: a single-org token for org 2 must
    bind org 2 — any 'default to org 1 / first org' shortcut shows org
    1's people here."""
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=[RIVAL_ALIAS])
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert _names(r) == {"Org Two Person"}


def test_the_header_selects_among_memberships(
    db_engine, tmp_path, two_org_world
):
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(
        roles=["org_admin"], organizations=[DEFAULT_ORG_ALIAS, RIVAL_ALIAS]
    )
    r = client.get(
        "/api/employees",
        headers={
            "Authorization": f"Bearer {token}",
            ACTIVE_ORG_HEADER: RIVAL_ALIAS,
        },
    )
    assert _names(r) == {"Org Two Person"}


def test_multi_org_token_without_header_is_400(
    db_engine, tmp_path, two_org_world
):
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(
        roles=["org_admin"], organizations=[DEFAULT_ORG_ALIAS, RIVAL_ALIAS]
    )
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 400


def test_non_member_and_unknown_alias_refuse_identically(
    db_engine, tmp_path, two_org_world
):
    """The oracle pin AND the dropped-membership-check mutant killer in
    one: org 2 EXISTS (a member-check-less mutant would happily resolve
    it and serve org 2's data), and the refusal for it is byte-identical
    to the refusal for an alias that exists nowhere — a caller cannot
    distinguish 'not yours' from 'not real'."""
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=[DEFAULT_ORG_ALIAS])
    responses = []
    for alias in (RIVAL_ALIAS, "no-such-org-anywhere"):
        r = client.get(
            "/api/employees",
            headers={
                "Authorization": f"Bearer {token}",
                ACTIVE_ORG_HEADER: alias,
            },
        )
        assert r.status_code == 403
        assert RIVAL_ALIAS not in r.text
        assert "no-such-org-anywhere" not in r.text
        responses.append((r.status_code, r.json()))
    assert responses[0] == responses[1]


def test_a_member_alias_with_no_db_org_refuses_like_a_non_member(
    db_engine, tmp_path, two_org_world
):
    """Provisioned in Keycloak but not (yet) in the DB: the claim admits
    the alias, the DB answers nothing — same 403, same words."""
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=["kc-only-org"])
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
    assert "kc-only-org" not in r.text


def test_orgless_token_is_refused_on_an_org_surface(
    db_engine, tmp_path, two_org_world
):
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=None)
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


def test_a_malformed_org_claim_is_401_not_500(
    db_engine, tmp_path, two_org_world
):
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(
        roles=["org_admin"], organizations=None,
        extra_claims={"organization": {"pilot-hotel-group": {}}},
    )
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401


def test_a_numeric_alias_is_an_alias_not_an_org_id(
    db_engine, tmp_path, two_org_world
):
    """Kills the trust-the-token mutant: '1' is a perfectly valid org_id
    and NO org's alias — resolution that ever int()s a token value would
    bind the founding org here; the real path finds no row and refuses."""
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=["1"])
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


def test_an_unresolvable_org_upload_leaves_nothing_in_the_inbox(
    db_engine, tmp_path, two_org_world
):
    """/ingest is the one handler with a filesystem side effect: the
    upload must NOT touch the inbox until the session opens (which is
    what fires the deferred alias -> org_id resolution). Bytes written
    before the 403 would dangle un-filed where a later `usali watch`
    drains them under the FOUNDING org — attacker-supplied content
    landing in org 1's data."""
    verifier, mint = make_authkit()
    _, client = _client(db_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=["kc-only-org"])
    r = client.post(
        "/ingest",
        files={"file": ("intruder.pdf", b"%PDF-1.4 attacker bytes")},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    inbox = tmp_path / "in"
    assert not inbox.exists() or list(inbox.iterdir()) == []


def test_an_org2_write_stamps_org_2_and_refuses_a_cross_org_explicit_id(
    app_role_engine, db_session, two_org_world
):
    """The write-side wall, EVOLVED for L6a (was
    test_an_org2_write_refuses_rather_than_landing_in_org_1, which pinned
    the PRE-stamping reality: an org-2 write died fail-closed on the
    server default). L6a's before_flush stamping wall makes an org-2-bound
    session WRITE correctly into org 2 — and carries the old test's
    fail-closed meaning forward for the two ways a write can still go
    wrong. The security posture is unchanged (never a cross-org landing);
    only its shape moved from "org != 1 cannot write at all" to "org != 1
    writes its OWN org, and a wrong org refuses loudly and cleanly".

    (a) an org-2-bound INSERT with no explicit org_id LANDS in org 2 — the
        stamp beats the server default ('1'), so the DB wall's WITH CHECK
        (which refused the default-1 row before) is now satisfied;
    (b) an org-2-bound INSERT that EXPLICITLY hardcodes org_id=1 refuses
        LOUDLY with OrgContextMismatch BEFORE the flush reaches the DB —
        defense-in-depth ABOVE the RLS WITH CHECK: a clean application
        error naming nothing sensitive, not a raw ProgrammingError/500;
    (c) a contextless (instrumented-but-unbound) session inserting an
        org-scoped row still refuses loudly (MissingOrgContext) — never a
        silently unscoped write.
    """
    factory = make_session_factory(app_role_engine)

    # (a) unset org_id is stamped from the session and the row lands in org 2.
    with factory() as s:
        bind_org_context(s, 2)
        ev = AuditEvent(
            actor_subject="l6a-stamp-probe", action="probe",
            resource_type="organization", resource_id="2",
        )
        s.add(ev)
        s.flush()
        assert ev.org_id == 2  # stamped, not left to the server default
        s.commit()
    landed = db_session.execute(
        select(AuditEvent).where(AuditEvent.actor_subject == "l6a-stamp-probe")
    ).scalars().all()
    assert [e.org_id for e in landed] == [2]  # in org 2, not org 1

    # (b) an explicit cross-org org_id refuses BEFORE the DB — a clean
    #     OrgContextMismatch, not the raw ProgrammingError the DB wall
    #     would have raised had the row reached it.
    with factory() as s:
        bind_org_context(s, 2)
        s.add(AuditEvent(
            actor_subject="l6a-mismatch-probe", action="probe",
            resource_type="organization", resource_id="1", org_id=1,
        ))
        with pytest.raises(OrgContextMismatch):
            s.flush()
        s.rollback()
    assert db_session.execute(
        select(AuditEvent).where(
            AuditEvent.actor_subject == "l6a-mismatch-probe"
        )
    ).scalars().all() == []

    # (c) instrumented but never bound: an org-scoped write still refuses
    #     loudly rather than landing silently unscoped.
    with factory() as s:
        instrument_org_wall(s)
        s.add(AuditEvent(
            actor_subject="l6a-contextless-probe", action="probe",
            resource_type="organization", resource_id="?",
        ))
        with pytest.raises(MissingOrgContext):
            s.flush()
        s.rollback()
    assert db_session.execute(
        select(AuditEvent).where(
            AuditEvent.actor_subject == "l6a-contextless-probe"
        )
    ).scalars().all() == []


def test_organization_insert_is_exempt_from_the_stamp_and_mismatch_rules(
    db_session, two_org_world
):
    """`Organization` is org-scoped by its OWN primary key (not the
    OrgScoped mixin), so its org_id is the IDENTITY being created, not a
    tenant reference — the before_flush stamp/mismatch logic must EXEMPT
    it. Provisioning (L6b) inserts the new org's row while bound to the
    FOUNDING org, so a founding-bound (org 1) session inserting
    Organization(org_id=2) must NOT trip OrgContextMismatch. Pinned on the
    superuser session (bypasses RLS) so the exemption is isolated from the
    DB wall: if a mutant makes the rule apply to Organization, org_id=2 vs
    session-org 1 raises OrgContextMismatch and this flush dies.

    (L6b concern, reported separately: the org RLS WITH CHECK still blocks
    a cross-org Organization insert on the APP role — provisioning must
    write the org row owner-side. This pin covers only the app-level wall.)
    """
    bind_org_context(db_session, FOUNDING_ORG_ID)  # founding-bound, org 1
    db_session.add(Organization(
        org_id=3, name="Provisioned Org Three", kc_org_alias="org-three"
    ))
    db_session.flush()  # must NOT raise OrgContextMismatch
    # Read back via text() (bypasses the ORM read wall, which — since we
    # bound org 1 — would otherwise filter this org-3 row out) on the
    # superuser session (bypasses RLS): the row landed with org_id 3, its
    # own identity, neither stamped to 1 nor refused as a mismatch.
    assert db_session.execute(
        text("SELECT org_id FROM organization WHERE org_id = 3")
    ).scalar_one() == 3
    db_session.rollback()


def test_a_core_insert_bypasses_before_flush_and_rides_the_db_wall_alone(
    app_role_engine, db_session, two_org_world
):
    """The write-side analogue of the L2 read-side Core-statement note: the
    before_flush stamping wall is ORM unit-of-work ONLY. A Core
    `session.execute(insert(...))` never enters the ORM flush, so it is
    NOT stamped — the row keeps its server default ('1'), and on an
    org-2-bound session the DB wall's WITH CHECK is the sole guard that
    refuses it (exactly the pre-L6a shape, still standing for Core paths:
    owner-side seeds/migrations use Core inserts and rely on the default).
    """
    from sqlalchemy import insert

    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, 2)
        with pytest.raises(ProgrammingError, match="row-level security"):
            s.execute(insert(AuditEvent).values(
                actor_subject="l6a-core-probe", action="probe",
                resource_type="organization", resource_id="2",
            ))
            s.flush()
        s.rollback()
    assert db_session.execute(
        select(AuditEvent).where(AuditEvent.actor_subject == "l6a-core-probe")
    ).scalars().all() == []


# ------------------------------------------------------- the binding pin


def test_the_validated_org_and_only_it_reaches_the_session_var(
    db_engine, two_org_world
):
    """The factory's sessions carry org 2 on BOTH walls' inputs — the
    session.info context the ORM criteria read and the transaction-local
    RLS variable — and the alias variable does NOT leak into them (the
    lookup's window died with the lookup transaction)."""
    factory = ActiveOrgSessionFactory(
        make_session_factory(db_engine), RIVAL_ALIAS,
        unknown_alias_error=RuntimeError,
    )
    with factory() as s:
        assert current_org_id(s) == 2
        assert s.execute(
            text("SELECT current_setting(:v, true)"), {"v": RLS_ORG_VAR}
        ).scalar() == "2"
        assert s.execute(
            text("SELECT NULLIF(current_setting(:v, true), '')"),
            {"v": RLS_ALIAS_VAR},
        ).scalar() is None


def test_resolution_happens_once_per_request_factory(
    db_engine, two_org_world, monkeypatch
):
    import usali.tenancy as tenancy_module

    calls = []
    real = tenancy_module.resolve_org_id

    def counting(session, alias):
        calls.append(alias)
        return real(session, alias)

    monkeypatch.setattr(tenancy_module, "resolve_org_id", counting)
    factory = ActiveOrgSessionFactory(
        make_session_factory(db_engine), RIVAL_ALIAS,
        unknown_alias_error=RuntimeError,
    )
    with factory() as s1:
        assert current_org_id(s1) == 2
    with factory() as s2:
        assert current_org_id(s2) == 2
    assert calls == [RIVAL_ALIAS]


def test_the_device_surface_stays_founding_org_bound(db_engine, tmp_path):
    """The kiosk DEVICE factory (no OIDC claim to resolve) binds the
    founding org — stated, not accidental; kiosk multi-org is deferred
    with provisioning (L6+)."""
    verifier, _ = make_authkit()
    app, _client_unused = _client(db_engine, tmp_path, verifier)
    with app.state.device_session_factory() as s:
        assert current_org_id(s) == FOUNDING_ORG_ID


# ----------------------------------- the full stack, on the app role


@pytest.fixture(scope="module")
def app_role_engine(db_url):
    engine = make_engine(app_role_url(db_url))
    yield engine
    engine.dispose()


def test_end_to_end_on_the_rls_bound_role(
    app_role_engine, tmp_path, two_org_world
):
    """The serving stack exactly as the cloud runs it: the app role (no
    BYPASSRLS), the alias-policy lookup, and both walls — org 2's token
    reads org 2's people and nothing else."""
    verifier, mint = make_authkit()
    _, client = _client(app_role_engine, tmp_path, verifier)
    token = mint(roles=["org_admin"], organizations=[RIVAL_ALIAS])
    r = client.get(
        "/api/employees", headers={"Authorization": f"Bearer {token}"}
    )
    assert _names(r) == {"Org Two Person"}


# ------------------------------------------- the alias policy, directly


def test_resolve_org_id_works_pre_binding_on_the_app_role(
    app_role_engine, two_org_world
):
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        assert resolve_org_id(s, RIVAL_ALIAS) == 2
    with factory() as s:
        assert resolve_org_id(s, DEFAULT_ORG_ALIAS) == 1
    with factory() as s:
        assert resolve_org_id(s, "no-such-org") is None


def test_the_alias_door_shows_one_exact_row_and_unset_shows_none(
    app_role_engine, two_org_world
):
    """The enumeration pin: with the variable set to one alias, the OTHER
    org's row stays invisible; with it unset, the door adds nothing to
    the (fail-closed, zero-row) org_id policy."""
    with app_role_engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM organization")
        ).scalar() == 0
    with app_role_engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text("SELECT set_config(:v, :a, true)"),
                {"v": RLS_ALIAS_VAR, "a": DEFAULT_ORG_ALIAS},
            )
            rows = conn.execute(
                text("SELECT org_id, kc_org_alias FROM organization")
            ).all()
            assert [(r.org_id, r.kc_org_alias) for r in rows] == [
                (1, DEFAULT_ORG_ALIAS)
            ]


def test_the_alias_door_cannot_write(app_role_engine, two_org_world):
    """FOR SELECT only: with the alias variable set (and no org bound),
    an INSERT refuses on the org_id policy's WITH CHECK and an UPDATE
    finds no row to touch."""
    with pytest.raises(ProgrammingError, match="row-level security"):
        with app_role_engine.connect() as conn:
            with conn.begin():
                conn.execute(
                    text("SELECT set_config(:v, :a, true)"),
                    {"v": RLS_ALIAS_VAR, "a": DEFAULT_ORG_ALIAS},
                )
                conn.execute(text(
                    "INSERT INTO organization (org_id, name, kc_org_alias) "
                    "VALUES (99, 'Intruder Org', 'intruder-org')"
                ))
    with app_role_engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text("SELECT set_config(:v, :a, true)"),
                {"v": RLS_ALIAS_VAR, "a": DEFAULT_ORG_ALIAS},
            )
            result = conn.execute(text(
                "UPDATE organization SET name = 'renamed' "
                "WHERE kc_org_alias = :a"
            ), {"a": DEFAULT_ORG_ALIAS})
            assert result.rowcount == 0


def test_the_policy_is_select_only_in_the_catalog(db_engine):
    with db_engine.connect() as conn:
        cmd, qual = conn.execute(text(
            "SELECT cmd, qual FROM pg_policies WHERE policyname = :p",
        ), {"p": "org_alias_lookup"}).one()
    assert cmd == "SELECT"
    assert RLS_ALIAS_VAR in qual and "kc_org_alias" in qual


# ------------------------------------------------------------- the seed


def test_ensure_default_org_stamps_the_alias(db_session):
    org_id = ensure_default_org(db_session)
    alias = db_session.execute(
        select(Organization.kc_org_alias).where(Organization.org_id == org_id)
    ).scalar_one()
    assert alias == DEFAULT_ORG_ALIAS == "pilot-hotel-group"


def test_a_reseed_never_blanks_an_operator_set_alias(db_session):
    db_session.add(Organization(
        org_id=1, name="Pilot Hotel Group", kc_org_alias="operator-chosen"
    ))
    db_session.commit()
    ensure_default_org(db_session)
    assert db_session.execute(
        select(Organization.kc_org_alias).where(Organization.org_id == 1)
    ).scalar_one() == "operator-chosen"


def test_the_seed_backfills_a_null_alias(db_session):
    """The upgrade path: an org row from before L3 (alias NULL) converges
    to the dev alias on the next seed run."""
    db_session.add(Organization(org_id=1, name="Pilot Hotel Group"))
    db_session.commit()
    ensure_default_org(db_session)
    assert db_session.execute(
        select(Organization.kc_org_alias).where(Organization.org_id == 1)
    ).scalar_one() == DEFAULT_ORG_ALIAS


# ------------------------------------------------------------ structure


def test_l3_sits_on_the_single_alembic_chain():
    """One head, with l3a0aliaslookup an ancestor of it — the L1/L2
    convention: the exact-head pin lives with the NEWEST migration's own
    module (test_l4_org_grants since L4)."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1
    assert "l3a0aliaslookup" in {
        rev.revision for rev in script.walk_revisions()
    }


def test_the_alias_var_is_pinned_as_a_literal():
    assert RLS_ALIAS_VAR == "app.org_alias"
    assert ACTIVE_ORG_HEADER == "X-Active-Org"
