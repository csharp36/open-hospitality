"""OH-17: per-tenant integration credentials (design D-OH17.1, D-OH17.5)."""

import base64
import importlib.util

import pytest
from sqlalchemy import String, text
from sqlalchemy.exc import IntegrityError

from usali import integrations as integ
from usali.crypto import EncryptedString
from usali.db import make_session_factory
from usali.models import Base, OrgIntegrationCredential
from usali.qbo_client import QboClient
from usali.tenancy import FOUNDING_ORG_ID, OrgBoundSessionFactory

from tests.credentials import plant_credential, unreadable_ciphertext


def _load_migration(mod_name: str, path: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def _check_parity_pair() -> tuple[str, str]:
    """(model CHECK sqltext, migration CHECK literal), as strings."""
    migration = _load_migration(
        "b3a0integcred",
        "migrations/versions/b3a0integcred_org_integration_credential.py",
    )
    constraint = next(
        c for c in OrgIntegrationCredential.__table__.constraints
        if getattr(c, "name", None) == "ck_org_integration_credential_provider_fields"
    )
    return str(constraint.sqltext), migration._CHECK



def test_the_table_is_registered():
    assert "org_integration_credential" in Base.metadata.tables


def test_org_settings_is_gone():
    """D-OH17.1: crm_provider was OrgSettings' only column, so absorbing it
    into the credential row leaves an empty table — and an empty table is
    where the next drift grows back."""
    assert "org_settings" not in Base.metadata.tables
    assert not hasattr(__import__("usali.models", fromlist=["x"]), "OrgSettings")


def test_org_id_is_part_of_the_primary_key():
    """The OrgChecklistOverride shape: org-scoped by its own composite key,
    so both L2 walls confine it automatically."""
    pk = {c.name for c in OrgIntegrationCredential.__table__.primary_key}
    assert pk == {"org_id", "integration"}


def test_secrets_are_encrypted_and_identifiers_are_not():
    """ADR-005: only actual secrets pay the encryption tax. `refresh_token`,
    `api_token`, `client_secret`, `subscription_key`, and `api_key` are bearer
    material — anyone holding one can act as the tenant against the provider
    — so they are `EncryptedString`. `realm_id`, `company_id`, and `client_id`
    are identifiers, not secrets: they are useless without the matching
    secret, and reading one in plaintext during a support conversation is
    worth more than encrypting it. This split is deliberate — if you are
    "helpfully" encrypting a company id, or leaving a new secret column
    plaintext because it "doesn't look secret enough," this test is the place
    that disagrees with you."""
    table = OrgIntegrationCredential.__table__
    encrypted_columns = {
        "refresh_token",
        "api_token",
        "client_secret",
        "subscription_key",
        "api_key",
    }
    plaintext_columns = {"realm_id", "company_id", "client_id"}

    for name in encrypted_columns:
        assert isinstance(table.c[name].type, EncryptedString), name
    for name in plaintext_columns:
        assert isinstance(table.c[name].type, String), name
        assert not isinstance(table.c[name].type, EncryptedString), name


# All three go through raw `text()` rather than the ORM on purpose: the claim
# is that the DATABASE refuses these rows, independently of the app import
# (D-OH17.5). Each `match=` names the CHECK explicitly — without it a row that
# happens to collide with a seeded PK would raise IntegrityError too, and the
# test would pass while proving nothing about the CHECK. `Session.execute` on
# a `text()` INSERT emits immediately, so the refusal lands on that statement;
# there is no later flush to wait for.
_CHECK = "ck_org_integration_credential_provider_fields"


def test_the_check_refuses_a_gusto_row_carrying_an_api_key(db_session, founding_org):
    """D-OH17.5: the DB refuses a malformed credential row independently of
    the app import. The 'must be NULL' half is what stops a stale api_key
    surviving a switch from Tripleseat to Delphi."""
    with pytest.raises(IntegrityError, match=_CHECK):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, api_token, company_id, api_key, connected_by) "
            "VALUES (1, 'payroll', 'gusto', 'x', 'c1', 'leftover', 'sub')"
        ))


def test_the_check_refuses_a_row_with_no_secret(db_session, founding_org):
    """The other half: a provider with NO credential at all. D-OH17.1 says the
    row IS the connection, so a provider name on its own is not a connection —
    it is the `org_settings.crm_provider` split this table exists to end."""
    with pytest.raises(IntegrityError, match=_CHECK):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, connected_by) "
            "VALUES (1, 'demand_feed', 'delphi', 'sub')"
        ))


def test_the_check_refuses_a_provider_from_another_integration(db_session, founding_org):
    """`integration` and `provider` arrive as INDEPENDENT inputs from the
    connect endpoint, so nothing in the column types stops a caller pairing
    them wrongly. Without this half of the CHECK a QBO refresh token could sit
    in the demand-feed slot, and `checklist._probe_demand_feed` — which asks
    only whether a demand_feed row exists — would read it as connected."""
    with pytest.raises(IntegrityError, match=_CHECK):
        db_session.execute(text(
            "INSERT INTO org_integration_credential "
            "(org_id, integration, provider, realm_id, refresh_token, connected_by) "
            "VALUES (1, 'demand_feed', 'qbo', 'r1', 'tok', 'sub')"
        ))


def test_every_spec_names_at_least_one_secret():
    for spec in integ.PROVIDERS:
        assert spec.secret_fields, spec.provider


def test_specs_cover_exactly_the_three_integrations():
    assert {s.integration for s in integ.PROVIDERS} == set(integ.INTEGRATIONS)


def test_spec_for_is_keyed_on_the_pair_not_the_provider_alone():
    """'qbo' is only legal under 'accounting' — the pair is the key, which is
    what the DB CHECK also enforces."""
    assert integ.spec_for("accounting", "qbo") is not None
    assert integ.spec_for("demand_feed", "qbo") is None


def test_the_registry_mirrors_the_crm_provider_closed_set():
    """crm_feed.CRM_PROVIDERS stays the source for demand-feed provider names;
    a new adapter must not be reachable here without being added there."""
    from usali.crm_feed import CRM_PROVIDERS
    feed = {s.provider for s in integ.PROVIDERS if s.integration == integ.DEMAND_FEED}
    assert feed == set(CRM_PROVIDERS)


def _attempt_insert(db_session, values: dict[str, object]) -> None:
    """Insert one row inside its own SAVEPOINT, then always roll the
    savepoint back — win or lose. This function's callers only care whether
    the CHECK accepts or refuses a given shape, never about leaving rows
    behind for later cases to collide with on the (org_id, integration)
    primary key. The rollback is unconditional (`finally`), not an
    error-only recovery: Postgres aborts the whole surrounding transaction on
    any statement error, so a bare `execute` that raises would poison every
    later assertion in the same test — `begin_nested` scopes the damage to
    just this one attempt, whether it succeeded or failed."""
    cols = ", ".join(values)
    placeholders = ", ".join(f":{k}" for k in values)
    savepoint = db_session.begin_nested()
    try:
        db_session.execute(
            text(
                f"INSERT INTO org_integration_credential ({cols}) "
                f"VALUES ({placeholders})"
            ),
            values,
        )
    finally:
        savepoint.rollback()


def test_the_models_check_is_byte_identical_to_the_migrations(_check_parity_pair):
    """The schema-mirror rule has a THIRD copy nobody was checking.

    `PROVIDERS`, the model's `__table_args__` CHECK, and the migration's
    `_CHECK` must all say the same thing. `test_the_check_agrees_with_the_registry`
    crosses the registry to the DATABASE — which is built by
    `alembic upgrade head` (conftest), so it exercises the MIGRATION's copy
    and never reads the model's literal at all. `create_all` appears nowhere
    in this repo, so nothing else materialises it either.

    The migration's own comment claimed alembic's `compare_metadata` parity
    check covered this. It does not — measured in review 2026-08-31 by
    replacing the model's constraint outright and getting zero diffs back.
    So a mistyped `IS NULL` term in the model would sit undetected until
    someone autogenerated a revision or added a metadata-built fixture, at
    which point the "every other column must be NULL" half — the half that
    stops a previous provider's secret surviving a re-connect — would quietly
    weaken.

    Three lines, and the repo already has the idiom: `test_l2_rls_wall.py`
    loads a migration module to cross-pin the RLS predicate the same way."""
    model_check, migration_check = _check_parity_pair
    assert model_check == migration_check


@pytest.mark.parametrize(
    "spec", integ.PROVIDERS, ids=lambda s: f"{s.integration}:{s.provider}"
)
def test_the_check_agrees_with_the_registry(db_session, founding_org, spec):
    """`PROVIDERS` and `ck_org_integration_credential_provider_fields` are one
    rule written twice (D-OH17.5) — and until this test, nothing checked that
    the two copies agree. The four registry tests above never touch the DB;
    the three CHECK tests above assert on hardcoded literals independent of
    `PROVIDERS`. So a provider added to one and forgotten in the other passed
    every existing test, and the drift would only surface later as a row the
    database rejects, far from its cause. This is parametrized off PROVIDERS
    itself so it grows automatically when a provider is added, and a failure
    names the offending (integration, provider) pair.

    Positive case: exactly `spec.fields` populated, everything else NULL,
    must be ACCEPTED — this catches a spec that claims fewer fields than the
    CHECK actually demands. Negative cases: each field NOT in `spec.fields`,
    added one at a time on top of an otherwise-legal row, must be REFUSED —
    this catches a spec that claims fewer fields than the CHECK forbids.
    """
    # founding_org unconditionally seeds org 1's payroll and accounting rows
    # (property_registry._seed_integration_credentials) — delete whatever it
    # planted for THIS integration first, or this test's own insert collides
    # with the seed on the (org_id, integration) primary key and either PASSES
    # FOR THE WRONG REASON (positive case: the seed row already exists, so a
    # broken insert is never actually exercised) or fails with the wrong error
    # (negative case: PK violation, not the CHECK). Org 1 itself stays — it
    # already satisfies the FK, so no other org needs to exist for this test.
    db_session.execute(
        text(
            "DELETE FROM org_integration_credential "
            "WHERE org_id = 1 AND integration = :integration"
        ),
        {"integration": spec.integration},
    )

    base = {
        "org_id": 1,
        "integration": spec.integration,
        "provider": spec.provider,
        "connected_by": "sub",
    }
    legal = {field: "v" for field in spec.fields}

    # Positive: exactly spec.fields, nothing more — must succeed.
    _attempt_insert(db_session, {**base, **legal})

    # Negative: every OTHER credential column, added one at a time on top of
    # an otherwise-legal row — each must be refused BY THIS CHECK specifically.
    for field in integ.ALL_CREDENTIAL_FIELDS:
        if field in spec.fields:
            continue
        with pytest.raises(IntegrityError, match=_CHECK):
            _attempt_insert(db_session, {**base, **legal, field: "leftover"})


# ------------------------------------------------- resolution (Task 5)


# `unconnected_org` moved to tests/conftest.py (OH-17 Task 7):
# tests/test_checklist.py needs it too, and a fixture used by two modules
# belongs in conftest, not duplicated — the same reason `_connect` below
# stays importable rather than copied.


def _connect(session, integration, provider, **fields):
    """Insert a credential row directly, bypassing the API — for tests about
    resolution rather than about the write path.

    It COMMITS, and that is load-bearing. Every `resolve_*` and every
    `DbTokenStore` method opens its own session off the org-bound factory —
    a different connection from this one — so a merely flushed row is
    invisible to them and each test would silently assert "not connected".
    A `session.flush()` here is the plausible-looking change that makes this
    whole section vacuous."""
    session.add(OrgIntegrationCredential(
        org_id=1, integration=integration, provider=provider,
        connected_by="test-subject", **fields,
    ))
    session.commit()


def _raw_column(session, integration, column):
    """The value Postgres actually holds, with the ORM's decrypting type out
    of the picture — `text()` binds no result processor."""
    return session.execute(
        text(f"SELECT {column} FROM org_integration_credential "  # noqa: S608
             "WHERE integration = :integration"),
        {"integration": integration},
    ).scalar_one()


def test_resolve_returns_none_when_not_connected(org_bound_factory, unconnected_org):
    """"Not connected" is an ordinary state, not an error: the `resolve_*`
    functions answer None and let their callers refuse on their own terms
    (a payroll run 409s, the demand pull degrades to the OFF sentinel).
    Raising here would make the checklist's honest "open" state a 500."""
    assert integ.resolve_payroll(org_bound_factory) is None
    assert integ.resolve_qbo(org_bound_factory) is None
    assert integ.resolve_crm_feed(org_bound_factory) is None


@pytest.mark.parametrize(
    ("provider", "adapter", "fields"),
    [
        ("gusto", "GustoAdapter", {"api_token": "t", "company_id": "c"}),
        ("adp", "AdpAdapter", {"client_id": "ci", "client_secret": "cs"}),
    ],
)
def test_resolve_payroll_builds_the_named_adapter(
    org_bound_factory, db_session, unconnected_org, provider, adapter, fields
):
    """Both payroll branches, because they are not symmetric: Gusto takes
    api_token/company_id and ADP takes client_id/client_secret, and a
    copy-pasted keyword in either branch is a TypeError this catches."""
    _connect(db_session, "payroll", provider, **fields)
    resolved = integ.resolve_payroll(org_bound_factory)
    assert type(resolved.adapter).__name__ == adapter
    # The NAME rides back with the adapter and comes from the ROW. Callers
    # persist it as the ProviderEmployeeRef key, so an adapter that arrived
    # without its name (or with the wrong one) is a mis-pay, not a mislabel —
    # see `integrations.ResolvedPayroll`.
    assert resolved.provider_name == provider


@pytest.mark.parametrize(
    ("provider", "adapter", "fields"),
    [
        ("delphi", "DelphiAdapter", {"subscription_key": "s"}),
        ("tripleseat", "TripleseatAdapter", {"api_key": "k"}),
    ],
)
def test_resolve_crm_feed_builds_the_named_adapter(
    org_bound_factory, db_session, unconnected_org, provider, adapter, fields
):
    _connect(db_session, "demand_feed", provider, **fields)
    assert type(integ.resolve_crm_feed(org_bound_factory)).__name__ == adapter


def test_resolve_qbo_reads_the_realm_from_the_row_not_the_env(
    org_bound_factory, db_session, unconnected_org
):
    """D-OH17.3 draws the line: our Intuit APPLICATION id/secret and the base
    URL stay process-wide because they identify the app, but the realm is the
    tenant's own QuickBooks company. Asserting on the private attribute is
    deliberate — the realm is otherwise only observable by watching an
    outbound URL, and this is the seam where a `settings.qbo_realm_id`
    fallback would hide."""
    _connect(db_session, "accounting", "qbo", realm_id="realm-9", refresh_token="r0")
    client = integ.resolve_qbo(org_bound_factory)
    assert isinstance(client, QboClient)
    assert client._realm_id == "realm-9"


def test_resolve_qbo_hands_the_client_a_db_backed_store(
    org_bound_factory, db_session, unconnected_org
):
    """The whole point of D-OH17.7: the client's token lineage must live in
    the row. A `StaticTokenStore` here would pass every other test in this
    file and still lose every rotation on restart."""
    _connect(db_session, "accounting", "qbo", realm_id="r", refresh_token="bootstrap")
    client = integ.resolve_qbo(org_bound_factory)
    assert isinstance(client._tokens, integ.DbTokenStore)
    assert client._tokens.load() == "bootstrap"


def test_the_token_store_survives_being_rebuilt(
    org_bound_factory, db_session, unconnected_org
):
    """Durability, which is the bug OH-17 actually fixes: a SECOND store over
    the same org — the stand-in for a restarted process or a second worker —
    reads the rotated token, not the bootstrap one. Before OH-17 the lineage
    died with the client and the next push `invalid_grant`ed."""
    _connect(db_session, "accounting", "qbo", realm_id="r", refresh_token="tok-0")
    integ.DbTokenStore(org_bound_factory).store("tok-1")
    assert integ.DbTokenStore(org_bound_factory).load() == "tok-1"


def test_the_rotated_token_is_encrypted_at_rest(
    org_bound_factory, db_session, unconnected_org
):
    """ADR-005 applies to the token the STORE writes, not just the one the
    connect endpoint wrote. `store()` goes through the mapped attribute so
    the `EncryptedString` bind processor runs; a raw `text()` UPDATE — the
    obvious "just one statement" optimisation — would write the rotated
    token to disk in plaintext and every other test here would still pass."""
    _connect(db_session, "accounting", "qbo", realm_id="r", refresh_token="tok-0")
    integ.DbTokenStore(org_bound_factory).store("rotated-s3cret")
    raw = _raw_column(db_session, "accounting", "refresh_token")
    assert raw != "rotated-s3cret"
    assert "rotated-s3cret" not in raw


def test_the_token_store_refuses_when_accounting_is_not_connected(
    org_bound_factory, unconnected_org
):
    """`load`/`store` are called from inside a push that has already decided a
    connection exists, so absence there is a broken invariant, not the
    ordinary "not connected" the `resolve_*` functions answer None for."""
    store = integ.DbTokenStore(org_bound_factory)
    with pytest.raises(integ.IntegrationNotConfigured):
        store.load()
    with pytest.raises(integ.IntegrationNotConfigured):
        store.store("anything")


def test_credentials_are_encrypted_at_rest(db_session, unconnected_org):
    """ADR-005: a DB dump must not yield the token. The ORM decrypts, so the
    assertion reads the raw column."""
    _connect(db_session, "demand_feed", "delphi", subscription_key="s3cret")
    raw = _raw_column(db_session, "demand_feed", "subscription_key")
    assert raw != "s3cret"
    assert "s3cret" not in raw


def test_each_payroll_credential_lands_in_its_own_slot(
    org_bound_factory, db_session, unconnected_org
):
    """Asserting the adapter's TYPE catches a mis-keyed constructor argument
    (that is a TypeError) but not a SWAPPED one: `api_token=row.company_id,
    company_id=row.api_token` builds a perfectly good GustoAdapter that
    authenticates with the company id. Only the two-field providers can have
    this bug — Delphi and Tripleseat carry a single credential each, so there
    is nothing to swap and no assertion of this kind is worth its coupling
    there.

    The observables are private because that is where the values actually go;
    the alternative is asserting on an outbound request, which would need a
    transport double per provider to prove a two-line mapping. The deliberately
    distinguishable values are what make the swap visible."""
    _connect(db_session, "payroll", "gusto", api_token="tok-A", company_id="co-B")
    gusto = integ.resolve_payroll(org_bound_factory).adapter
    assert gusto._company_id == "co-B"
    assert gusto._http.headers["Authorization"] == "Bearer tok-A"

    # Scoped, for the same reason `unconnected_org` scopes its delete.
    db_session.execute(
        text("DELETE FROM org_integration_credential WHERE org_id = :org"),
        {"org": FOUNDING_ORG_ID},
    )
    db_session.commit()
    _connect(db_session, "payroll", "adp", client_id="id-A", client_secret="sec-B")
    adp = integ.resolve_payroll(org_bound_factory).adapter
    # ADP folds both into one base64 blob, so the ORDER inside it is the only
    # place a swap shows up at all.
    basic = adp._basic_auth.removeprefix("Basic ")
    assert base64.b64decode(basic).decode() == "id-A:sec-B"


@pytest.mark.parametrize(
    ("resolve", "integration", "provider", "fields"),
    [
        ("resolve_payroll", "payroll", "gusto", {"api_token": "t", "company_id": "c"}),
        ("resolve_qbo", "accounting", "qbo", {"realm_id": "r", "refresh_token": "t"}),
        ("resolve_crm_feed", "demand_feed", "delphi", {"subscription_key": "s"}),
    ],
)
def test_an_unknown_provider_raises_instead_of_reading_as_disconnected(
    org_bound_factory, db_session, unconnected_org, monkeypatch,
    resolve, integration, provider, fields,
):
    """The three `raise RuntimeError` branches, which the comments beside them
    call load-bearing and which nothing else reaches.

    They are unreachable through the DB by design — the CHECK is the schema
    mirror of PROVIDERS, so a row naming an unknown provider cannot be
    inserted. That is exactly why the branch needs a test rather than being
    deleted as dead: it guards the state where someone has added a provider to
    the CHECK (and its migration) and forgotten this module. Returning None
    there would read as "not connected", and the tenant's connected
    integration would silently stop running instead of failing by name.

    So the row is real and legal, and only the PROVIDER is falsified, on the
    object `credential_for` hands back — the narrowest lie that reaches the
    branch."""
    _connect(db_session, integration, provider, **fields)
    real = integ.credential_for

    def rogue(session, wanted):
        row = real(session, wanted)
        if row is not None:
            row.provider = "someday-provider"
        return row

    monkeypatch.setattr(integ, "credential_for", rogue)
    with pytest.raises(RuntimeError, match="someday-provider"):
        getattr(integ, resolve)(org_bound_factory)


# ------------------------------------------- two-org isolation (Task 12)
#
# The claim OH-17 has to earn: one tenant's credentials are unreachable from
# another tenant's session. Everything below runs on `app_role_engine` — the
# RLS-bound, non-owner `usali_app` role — and NOT on the `db_engine`
# superuser these tests otherwise use. That is the whole point of this
# section: the ORM criteria hook is SELECT-only (tenancy.py, decision 2), so
# `update()`/`delete()` ride the DATABASE wall alone, and a superuser
# connection BYPASSES RLS no matter what the policy says. On `db_engine`
# these tests would pass over a table whose `org_wall` policy was never
# created — certifying a wall nobody checked.
#
# Each test carries a POSITIVE CONTROL (the org DOES see / DID change its own
# row) beside every cross-org assertion. An isolation test that passes
# because the query returned nothing for an unrelated reason — the wrong org
# bound, an empty table, a filter that never matched — is worse than no test.


def _app_factory_for(app_role_engine, org_id):
    """An org-bound session factory over the APP-ROLE engine.

    The precedent is `test_l2_rls_wall.test_the_app_factory_binds_the_founding_org`
    (which builds the same wrapper over `db_engine`, because its subject is the
    ORM hook rather than RLS). Built explicitly here rather than taken from a
    fixture because `two_tenant_world` returns a SimpleNamespace of ids
    (org2_id, org2_admin, org2_emp_id), not session factories — and
    `org_bound_factory` is pinned to the founding org over the superuser
    engine, which is neither org 2 nor RLS-bound."""
    return OrgBoundSessionFactory(make_session_factory(app_role_engine), org_id)


def _connect_bound(factory, integration, provider, **fields):
    """Connect one integration for whichever org `factory` is bound to.

    Deliberately NOT `_connect` above: that helper hardcodes `org_id=1`, which
    on an org-2-bound session is refused by the write wall
    (`tenancy.OrgContextMismatch`). Omitting org_id entirely is the correct
    shape — `_stamp_wall` stamps it from the session's own context, which is
    exactly how a request-path INSERT lands in the right tenant."""
    with factory() as session:
        session.add(OrgIntegrationCredential(
            integration=integration, provider=provider,
            connected_by="test-subject", **fields,
        ))
        session.commit()


def _visible(session, where="TRUE", **params):
    """How many credential rows this session can SEE, through raw SQL.

    `text()` bypasses the ORM wall entirely (tenancy.py's module docstring
    says so), so this counts what the DATABASE wall alone permits — the half
    an ORM-only assertion cannot reach."""
    return session.execute(
        text(f"SELECT count(*) FROM org_integration_credential WHERE {where}"),  # noqa: S608
        params,
    ).scalar_one()


def test_one_org_cannot_read_anothers_credentials(app_role_engine, two_tenant_world):
    """Both walls, both directions.

    `two_tenant_world` leaves org 1 connected to all three integrations (the
    D-OH17.15 seed bridge plants payroll + accounting; the world adds a delphi
    demand feed) and org 2 connected to nothing — so org 2 gets the credential
    org 1 does not have, and each side has something the other must not see.
    """
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    org_b = _app_factory_for(app_role_engine, two_tenant_world.org2_id)
    _connect_bound(org_b, integ.DEMAND_FEED, "tripleseat", api_key="org2-only-secret")

    with org_a() as session:
        # Positive control: org 1 sees its OWN feed, decrypted. Without this,
        # every assertion below would also pass on a session that could see
        # nothing at all.
        mine = integ.credential_for(session, integ.DEMAND_FEED)
        assert mine.provider == "delphi"
        assert _visible(session) > 0
        # Org 2's tripleseat row is not here. On THIS engine both walls are
        # up, so what follows proves the pair holds — not which one is doing
        # the work. Mutation testing (review, 2026-08-31) showed removing the
        # ORM hook entirely leaves every line below green, because RLS alone
        # satisfies them; the comment here used to claim it distinguished the
        # two. `test_the_orm_wall_alone_confines_credential_reads` is where
        # the ORM half is isolated.
        assert mine.api_key is None
        assert _visible(session, "provider = 'tripleseat'") == 0
        assert _visible(session, "org_id <> :org", org=FOUNDING_ORG_ID) == 0

    with org_b() as session:
        mine = integ.credential_for(session, integ.DEMAND_FEED)
        assert mine.provider == "tripleseat"
        assert mine.api_key == "org2-only-secret"  # positive control
        # Org 1 holds payroll and accounting rows; org 2 must read its own
        # absence, never org 1's presence.
        assert integ.credential_for(session, integ.PAYROLL) is None
        assert integ.credential_for(session, integ.ACCOUNTING) is None
        assert integ.has_credential(session, integ.PAYROLL) is False
        assert _visible(session) == 1
        assert _visible(session, "org_id <> :org", org=two_tenant_world.org2_id) == 0


def test_one_org_cannot_overwrite_anothers_credentials(app_role_engine, two_tenant_world):
    """The RLS wall STANDING ALONE, on the statement shape the ORM hook does
    not cover: a bare `UPDATE` with no org_id in its WHERE. The application
    wall is SELECT-only, so nothing but the policy's USING clause confines
    this — which is why it runs as the app role.

    The unscoped UPDATE below is the ATTACK, not a pattern: an org-scoped
    write carrying no org_id is precisely what this branch's `store()` hazard
    warns against. Do not copy it into production code."""
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    org_b = _app_factory_for(app_role_engine, two_tenant_world.org2_id)
    _connect_bound(org_b, integ.DEMAND_FEED, "tripleseat", api_key="k")

    with org_b() as session:
        session.execute(text(
            "UPDATE org_integration_credential SET connected_by = 'stolen'"
        ))
        session.commit()

    with org_a() as session:
        # `connected_by` and not a secret column on purpose: the CHECK pins
        # WHICH credential columns each provider may carry, so an UPDATE
        # setting `api_key` would be refused for org 1's delphi row on the
        # constraint — and would pass this test for the wrong reason.
        assert _visible(session, "connected_by = 'stolen'") == 0
        assert _visible(session) > 0

    with org_b() as session:
        # Positive control: the UPDATE really did run and really did match a
        # row. Without it, a statement that matched nothing anywhere (a typo,
        # a rolled-back transaction) would leave this test green.
        assert _visible(session, "connected_by = 'stolen'") == 1


def test_one_org_cannot_delete_anothers_credentials(app_role_engine, two_tenant_world):
    """The other half of the SELECT-only gap. A cross-tenant DELETE is the
    worse of the two — it destroys a connection rather than corrupting one,
    and leaves the victim's checklist item re-opened with nothing to explain
    it."""
    org_a = _app_factory_for(app_role_engine, FOUNDING_ORG_ID)
    org_b = _app_factory_for(app_role_engine, two_tenant_world.org2_id)
    _connect_bound(org_b, integ.DEMAND_FEED, "tripleseat", api_key="k")

    with org_a() as session:
        before = _visible(session)
        assert before > 0

    with org_b() as session:
        session.execute(text("DELETE FROM org_integration_credential"))
        session.commit()

    with org_a() as session:
        assert _visible(session) == before
    with org_b() as session:
        assert _visible(session) == 0  # positive control: it deleted its OWN


# --------------------------------- the undecryptable credential (Task 12)


# (integration, provider, the plain companion columns the CHECK demands, the
# secret column whose ciphertext is unreadable, the resolver that must refuse)
_UNREADABLE_CASES = [
    ("resolve_payroll", integ.PAYROLL, "gusto", "api_token", {"company_id": "c"}),
    ("resolve_qbo", integ.ACCOUNTING, "qbo", "refresh_token", {"realm_id": "r"}),
    ("resolve_crm_feed", integ.DEMAND_FEED, "delphi", "subscription_key", {}),
]


@pytest.mark.parametrize(
    ("resolve", "integration", "provider", "secret", "plain"),
    _UNREADABLE_CASES,
    ids=[case[1] for case in _UNREADABLE_CASES],
)
def test_an_undecryptable_credential_refuses_loudly(
    org_bound_factory, db_session, unconnected_org,
    resolve, integration, provider, secret, plain,
):
    """Design §7. ADR-005 records that rotating `field_encryption_key` makes
    existing ciphertext undecryptable, and that there is no envelope or key
    version to fall back on; this is where a tenant meets that.

    It must be a NAMED refusal — never a fallback to env, and above all never
    a silent "not connected". Folding it into `None` would re-open the
    checklist item and invite the operator to reconnect an integration that is
    fine, while the real cause is never surfaced (ADR-010: absence degrades to
    a NAMED blocker). All three resolvers, because each reads a different
    secret column and a per-branch `try` is exactly the kind of thing that
    gets added to two of three."""
    plant_credential(db_session, integration, provider,
                **{secret: unreadable_ciphertext("s3cret")}, **plain)

    with pytest.raises(integ.CredentialUnreadable) as caught:
        getattr(integ, resolve)(org_bound_factory)

    assert caught.value.integration == integration
    detail = str(caught.value)
    assert integration in detail
    # It carries no secret — not the plaintext (which nobody here can read
    # anyway) and not the stored ciphertext, which is still bearer material
    # to anyone holding the old key.
    assert "s3cret" not in detail


@pytest.mark.parametrize("planted", [
    "not-valid-ciphertext",  # long: fails base64 first
    "mock",                  # short: decodes to 3 bytes, dies on the nonce split
    "abcdefgh",              # short: decodes cleanly, still under one nonce
    "",                      # the empty column
    "s\u00e9cret",              # non-ASCII: b64decode refuses before anything else
])
def test_a_credential_that_is_not_ciphertext_at_all_refuses_the_same_way(
    planted, org_bound_factory, db_session, unconnected_org
):
    """The other shape the same column can hold: a value that is not even
    valid base64 — a hand-edited row, a half-restored backup, a column
    written before ADR-005 by something that did not encrypt.

    It refuses identically ON PURPOSE. `decrypt_str` raises
    `MalformedCiphertext` here and `InvalidTag` for a rotated key; a refusal
    that named only the latter would let this one through as a raw 500 in the
    middle of a push.

    PARAMETRISED, and that is the whole point of the second case. Until
    2026-08-31 the guard named `binascii.Error`, and this test passed only
    because of the literal it picked: a long value fails base64 and was
    caught, while a SHORT one decodes fine and died on the nonce split with a
    bare ValueError that escaped as a 500. The one-literal version of this
    test could not tell the two apart."""
    plant_credential(db_session, integ.DEMAND_FEED, "delphi",
                subscription_key=planted)
    with pytest.raises(integ.CredentialUnreadable):
        integ.resolve_crm_feed(org_bound_factory)


def test_the_token_store_refuses_an_unreadable_token(
    org_bound_factory, db_session, unconnected_org
):
    """`DbTokenStore` reads the same column mid-push and has its own
    `IntegrationNotConfigured` for absence — an unreadable token is NOT
    absence, and must not be reported as it. `store()` refuses too: it reads
    the row before writing, so it meets the same failure."""
    plant_credential(db_session, integ.ACCOUNTING, "qbo", realm_id="r",
                refresh_token=unreadable_ciphertext("tok-0"))
    store = integ.DbTokenStore(org_bound_factory)
    with pytest.raises(integ.CredentialUnreadable):
        store.load()
    with pytest.raises(integ.CredentialUnreadable):
        store.store("rotated")


def test_an_unreadable_credential_is_not_reported_as_disconnected(
    db_session, unconnected_org
):
    """The distinction, stated once directly: `connected_provider` must not
    answer '' — the OFF sentinel — for a row that exists but cannot be read.
    '' is what `crm_api` tests for feature-off, so folding the two together
    would degrade a broken key into a silent "demand feed is switched off"."""
    plant_credential(db_session, integ.DEMAND_FEED, "delphi",
                subscription_key=unreadable_ciphertext("s"))
    with pytest.raises(integ.CredentialUnreadable):
        integ.connected_provider(db_session, integ.DEMAND_FEED)


def test_the_orm_wall_alone_confines_credential_reads(db_engine, two_tenant_world):
    """The ORM half, ISOLATED — the assertion the test above cannot make.

    `db_session`'s engine connects as a superuser, so RLS is bypassed here and
    the `do_orm_execute` criteria hook is the only thing left standing. That
    makes this the one place a regression in the ORM wall is visible: with RLS
    covering for it, removing the hook changes nothing observable.

    Written after mutation testing found that deleting `tenancy`'s listener
    left the app-role isolation test green. Both walls are still required —
    the ORM one is SELECT-only, so the app-role test remains the only proof
    for UPDATE and DELETE."""
    org2 = OrgBoundSessionFactory(
        make_session_factory(db_engine), two_tenant_world.org2_id
    )
    with org2() as session:
        # Org 1's seeded payroll row exists and is invisible from org 2 by the
        # ORM route alone. The raw-SQL control below proves RLS is NOT the
        # thing hiding it.
        assert integ.credential_for(session, integ.PAYROLL) is None
        assert session.execute(
            text("SELECT count(*) FROM org_integration_credential "
                 "WHERE org_id = :org"),
            {"org": FOUNDING_ORG_ID},
        ).scalar_one() > 0, "RLS must be bypassed here, or this proves nothing"


def test_only_qbo_is_an_oauth_provider():
    """An EXACT set: a second OAuth provider must be added here deliberately,
    rather than falling through and being offered as an ordinary credential
    form. The flag's only consumer today is this test."""
    assert [s.provider for s in integ.PROVIDERS if s.oauth] == ["qbo"]


def test_every_provider_has_an_operator_facing_name():
    """An exact pairing over PROVIDERS. A provider with no product name falls
    back to its own key — the cosmetic failure `product_name`'s docstring
    describes — so a sixth provider fails here rather than reaching an
    operator as "adp2"."""
    for spec in integ.PROVIDERS:
        assert integ.product_name(spec.provider) != spec.provider
