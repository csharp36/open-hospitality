"""OH-17: per-tenant integration credentials (design D-OH17.1, D-OH17.5)."""

import pytest
from sqlalchemy import String, text
from sqlalchemy.exc import IntegrityError

from usali import integrations as integ
from usali.crypto import EncryptedString
from usali.mapping.property_registry import ensure_default_org
from usali.models import Base, OrgIntegrationCredential
from usali.qbo_client import QboClient


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


@pytest.fixture
def unconnected_org(db_session):
    """Org 1 exists and is connected to NOTHING.

    `founding_org` alone is the wrong starting state for every test below.
    `ensure_default_org` runs the D-OH17.15 seed bridge, which
    UNCONDITIONALLY plants org 1's payroll (gusto) and accounting (qbo)
    rows from the process env — so "not connected" would be false before the
    test began, and a `_connect(... 'payroll' ...)` would collide with the
    seed on the (org_id, integration) primary key rather than testing
    anything. Deleting the seeded rows is the smallest fix that keeps ONE
    org-creating implementation (the L1 rule) instead of hand-rolling a
    second `Organization` insert here.

    The org row itself must stay: every `_connect` below carries org_id = 1
    and would otherwise die on `fk_org_integration_credential_org`."""
    ensure_default_org(db_session)
    db_session.execute(text("DELETE FROM org_integration_credential"))
    db_session.commit()


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
    assert type(integ.resolve_payroll(org_bound_factory)).__name__ == adapter


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
