"""The read / connect / disconnect surface (OH-17 Task 10).

Two properties carry the whole feature, and every other test here exists to
keep them honest:

1. **No secret is ever returned.** The server CAN decrypt (ADR-005), so the
   read endpoint's discretion is a policy, not a consequence of the storage
   regime — which means only a test can hold it. `test_no_secret_is_ever_on
   _the_wire` plants a distinct sentinel in EVERY secret column and greps the
   whole response body, rather than asserting field-by-field: a future field
   added to `IntegrationModel` cannot quietly start echoing a key.

2. **Verify before persist (D-OH17.8).** A typo'd key must be a 422 and NOT a
   row, because `has_credential` — the checklist probe — is a cheap presence
   check by design. A stored-but-broken credential is a `done` badge over an
   integration that 502s on the tenant's first pay run, which is the drift
   D-B4.1 exists to prevent.

The write path's verification is an INJECTED seam
(`create_app(verify_integration=...)`), so the router tests never dial out.
That injection is also the risk: a seam nothing checks could be wired to a
no-op default and every router test would still pass. Two tests close that —
`test_the_default_verifier_is_the_real_one` pins the wiring, and the
`verify_credentials` section at the bottom drives the REAL adapters over the
in-process provider mocks.

Fixture note (D-OH17.15): `unconnected_org`, never `founding_org`. The latter
runs the seed bridge, which unconditionally plants org 1's payroll and
accounting rows from process env — "not connected" would be false before the
test began, and a `_connect` would collide on the composite primary key.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.credentials import plant_credential, unreadable_ciphertext
from tests.grants import grant_role
from tests.test_integrations import _connect
from usali import integrations
from usali.adp_adapter import AdpAdapter
from usali.adp_mock import create_mock_adp
from usali.crm_feed import CrmFeedError
from usali.db import make_session_factory
from usali.delphi_adapter import DelphiAdapter
from usali.delphi_mock import DELPHI_HOTEL_REF, create_mock_delphi
from usali.gusto_adapter import GustoAdapter
from usali.gusto_mock import create_mock_gusto
from usali.integrations import CannotVerify, verify_credentials
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import AuditEvent, OrgIntegrationCredential, Property
from usali.payroll_provider import ProviderError
from usali.qbo_client import SyncASGITransport
from usali.server import create_app
from usali.tripleseat_adapter import TripleseatAdapter
from usali.tripleseat_mock import TRIPLESEAT_LOCATION_ID, create_mock_tripleseat


class _Verifier:
    """The injected connect-time verification, recording what it was asked.

    `error` is settable AFTER the client is built (the `failing_verifier`
    fixture does exactly that), because the router reads the seam at call
    time — one spy object serves both the happy and the refusing world.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object], str | None]] = []
        self.error: Exception | None = None

    def __call__(
        self,
        integration: str,
        provider: str,
        values: dict[str, object],
        crm_ref: str | None,
    ) -> None:
        self.calls.append((integration, provider, dict(values), crm_ref))
        if self.error is not None:
            raise self.error


@pytest.fixture
def verifier_spy() -> _Verifier:
    return _Verifier()


@pytest.fixture
def failing_verifier(verifier_spy: _Verifier) -> _Verifier:
    """The same spy the client already holds, armed to refuse. A provider's
    own error type, because that is what the router narrows on."""
    verifier_spy.error = ProviderError("gusto credential verify failed (401)")
    return verifier_spy


def _client(db_engine, tmp_path, verifier, verify_integration) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        verify_integration=verify_integration,
    )
    return TestClient(app)


def _authenticated(client: TestClient, mint, db_session, role: str, sub: str) -> TestClient:
    # Both halves of the L4 gate: the realm's coarse claim on the token AND
    # the org-scoped `role_assignment` grant `require_grants` reads.
    grant_role(db_session, role, sub=sub, org_id=1)
    client.headers["Authorization"] = f"Bearer {mint(roles=[role], sub=sub)}"
    return client


@pytest.fixture
def integrations_client(
    db_engine, db_session, unconnected_org, tmp_path, verifier_spy
) -> TestClient:
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier, verifier_spy)
    return _authenticated(client, mint, db_session, "org_admin", "int-admin")


@pytest.fixture
def integrations_client_gm(
    db_engine, db_session, unconnected_org, tmp_path, verifier_spy
) -> TestClient:
    """A property_gm — an ORG-WIDE grant, so this is the strongest non-admin
    the system has. Connecting a tenant's payroll is not a GM's call."""
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier, verifier_spy)
    return _authenticated(client, mint, db_session, "property_gm", "int-gm")


def _row(db_session, integration: str) -> OrgIntegrationCredential | None:
    db_session.expire_all()  # the endpoint committed on ANOTHER connection
    return db_session.execute(
        select(OrgIntegrationCredential).where(
            OrgIntegrationCredential.integration == integration
        )
    ).scalar_one_or_none()


def _items(client: TestClient) -> dict[str, dict[str, object]]:
    resp = client.get("/api/integrations")
    assert resp.status_code == 200
    return {i["integration"]: i for i in resp.json()["items"]}


# ----------------------------------------------------------------- the read


def test_read_lists_every_integration_even_when_none_is_connected(integrations_client):
    """The read is a COMPLETE list, not the connected subset: the connect UI
    renders a card per integration, and an absent key would render nothing at
    all for the very integration the operator came to connect."""
    items = _items(integrations_client)
    assert set(items) == set(integrations.INTEGRATIONS)
    for item in items.values():
        assert item["connected"] is False
        assert item["provider"] is None
        assert item["identifiers"] == {}
        assert item["connected_at"] is None


def test_read_echoes_the_provider_and_its_plain_identifiers(
    integrations_client, db_session
):
    _connect(db_session, "payroll", "gusto", api_token="s3cret", company_id="c1")
    payroll = _items(integrations_client)["payroll"]
    assert payroll["connected"] is True
    assert payroll["provider"] == "gusto"
    # Being able to see WHICH company a tenant is pointed at is the value of
    # the read surface; being able to read the token back is only a liability.
    assert payroll["identifiers"] == {"company_id": "c1"}
    assert payroll["connected_at"] is not None


def test_no_secret_is_ever_on_the_wire(integrations_client, db_session):
    """The headline guarantee. The server holds the key (ADR-005), so nothing
    but this policy stops the read from decrypting — greps the WHOLE body so
    a newly added field cannot quietly start echoing one."""
    _connect(db_session, "payroll", "gusto",
             api_token="sentinel-gusto-token", company_id="c1")
    _connect(db_session, "accounting", "qbo",
             refresh_token="sentinel-refresh-token", realm_id="realm-9")
    _connect(db_session, "demand_feed", "delphi",
             subscription_key="sentinel-delphi-key")

    body = integrations_client.get("/api/integrations").text
    for secret in ("sentinel-gusto-token", "sentinel-refresh-token",
                   "sentinel-delphi-key"):
        assert secret not in body
    # ...while the non-secret identifiers ARE there, so the assertion above
    # cannot pass by the endpoint returning nothing at all.
    assert "realm-9" in body
    assert "c1" in body


def test_an_unreadable_credential_refuses_by_name_rather_than_500ing(
    integrations_client, db_session
):
    """ADR-005 reaches the connect surface too. A rotated
    `field_encryption_key` makes the row undecryptable, and the read decrypts
    every column the moment it loads the row — so without a refusal this page
    is an unhandled `InvalidTag`, on the ONE surface an operator would go to
    in order to understand what broke.

    The whole page refuses rather than rendering the readable two: the
    alternative is showing the unreadable integration as `connected: false`,
    which is the lie `CredentialUnreadable` exists to prevent. The remedy the
    message names still works while this 503s — `connect` upserts without
    ever reading the old row."""
    plant_credential(db_session, "demand_feed", "delphi",
                     subscription_key=unreadable_ciphertext("sentinel-key"))

    r = integrations_client.get("/api/integrations")
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "demand_feed" in detail
    assert "sentinel-key" not in detail


def test_a_non_admin_cannot_read(integrations_client_gm):
    """Every route is org_admin, the read included: the identifiers name the
    tenant's external accounts, and the list of what is NOT connected is a
    map of the workspace's gaps."""
    assert integrations_client_gm.get("/api/integrations").status_code == 403


# ---------------------------------------------------------------- the write


def test_connect_verifies_then_stores(integrations_client, db_session, verifier_spy):
    resp = integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "delphi", "subscription_key": "good-key"},
    )
    assert resp.status_code == 204
    row = _row(db_session, "demand_feed")
    assert row is not None
    assert row.provider == "delphi"
    assert row.subscription_key == "good-key"
    assert row.connected_by == "int-admin"
    assert verifier_spy.calls == [("demand_feed", "delphi", {"subscription_key": "good-key"}, None)]
    assert db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "integration_connected")
    ).scalar_one().resource_id == "demand_feed"


def test_connect_verifies_before_it_persists(integrations_client, failing_verifier):
    """D-OH17.8: a typo'd key must be a 422, not a `done` over an integration
    that 502s on first use. This is the assertion the whole write path exists
    for — `has_credential` is a presence check, so an unverified row IS a
    false `done`."""
    resp = integrations_client.put(
        "/api/integrations/payroll",
        json={"provider": "gusto", "api_token": "wrong", "company_id": "c1"},
    )
    assert resp.status_code == 422
    assert "gusto" in resp.json()["detail"]
    assert _items(integrations_client)["payroll"]["connected"] is False


def test_a_failed_verification_leaves_no_row_at_all(
    integrations_client, db_session, failing_verifier
):
    """The endpoint-level twin of the test above, read at the DATABASE rather
    than through the same router that refused. A write that landed and was
    then rolled back at the HTTP layer would pass the API-level assertion."""
    integrations_client.put(
        "/api/integrations/payroll",
        json={"provider": "gusto", "api_token": "wrong", "company_id": "c1"},
    )
    assert _row(db_session, "payroll") is None


def test_a_failed_verification_does_not_replace_a_working_credential(
    integrations_client, db_session, verifier_spy
):
    """PUT is a full replace, so "verify first" has to hold for the UPDATE arm
    too: a bad key pasted over a working connection must leave the working one
    standing, not delete-then-fail."""
    _connect(db_session, "payroll", "gusto", api_token="working", company_id="c1")
    verifier_spy.error = ProviderError("gusto credential verify failed (401)")
    resp = integrations_client.put(
        "/api/integrations/payroll",
        json={"provider": "adp", "client_id": "id", "client_secret": "typo"},
    )
    assert resp.status_code == 422
    row = _row(db_session, "payroll")
    assert row is not None
    assert row.provider == "gusto"
    assert row.api_token == "working"


def test_connect_refuses_a_provider_from_another_integration(
    integrations_client, verifier_spy
):
    """`spec_for` is keyed on the PAIR, the same rule the DB CHECK enforces.
    Refused before verification, so a mis-aimed provider never dials out."""
    resp = integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "qbo", "realm_id": "r", "refresh_token": "t"},
    )
    assert resp.status_code == 422
    assert verifier_spy.calls == []


def test_connect_refuses_a_missing_field_by_name(integrations_client, verifier_spy):
    resp = integrations_client.put(
        "/api/integrations/payroll", json={"provider": "gusto", "api_token": "t"},
    )
    assert resp.status_code == 422
    assert "company_id" in resp.json()["detail"]
    assert verifier_spy.calls == []


def test_connect_refuses_a_field_the_provider_does_not_take(
    integrations_client, verifier_spy
):
    """The CHECK's "must be NULL" half would refuse this at the DB with an
    IntegrityError 500; naming it here makes it a legible 422."""
    resp = integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "delphi", "subscription_key": "k", "api_key": "leftover"},
    )
    assert resp.status_code == 422
    assert "api_key" in resp.json()["detail"]
    assert verifier_spy.calls == []


def test_connect_refuses_a_non_string_credential(integrations_client, verifier_spy):
    """`extra="allow"` means the credential fields are un-typed by pydantic.
    An int reaching a String column is a psycopg 500; a nested object reaching
    the EncryptedString bind processor is another. Refuse at the door."""
    resp = integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "delphi", "subscription_key": {"nested": "object"}},
    )
    assert resp.status_code == 422
    assert verifier_spy.calls == []


def test_connect_nulls_the_previous_providers_fields(integrations_client, db_session):
    """A stale `api_key` surviving a switch from Tripleseat to Delphi is
    exactly what the CHECK's "must be NULL" half refuses — so if this
    regressed, the second PUT would 500 rather than store a mixed row. The
    assertion is on the columns, so it is also true one layer up."""
    assert integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "tripleseat", "api_key": "ts-key"},
    ).status_code == 204
    assert integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "delphi", "subscription_key": "delphi-key"},
    ).status_code == 204
    row = _row(db_session, "demand_feed")
    assert row is not None
    assert row.provider == "delphi"
    assert row.subscription_key == "delphi-key"
    assert row.api_key is None


def test_connect_refuses_an_unknown_integration(integrations_client):
    resp = integrations_client.put(
        "/api/integrations/telepathy", json={"provider": "gusto", "api_token": "t"},
    )
    assert resp.status_code == 404


def test_connect_passes_the_orgs_crm_ref_to_verification(
    integrations_client, db_session, verifier_spy
):
    """Every real CRM read is property-scoped, so verification needs a ref.
    Which property is immaterial — the credential is org-wide."""
    db_session.add(Property(org_id=1, property_id="HISJ", name="Hotel",
                            pms_source="OPERA", crm_ref=DELPHI_HOTEL_REF))
    db_session.commit()
    integrations_client.put(
        "/api/integrations/demand_feed",
        json={"provider": "delphi", "subscription_key": "k"},
    )
    assert verifier_spy.calls[0][3] == DELPHI_HOTEL_REF


# ----------------------------------------------------------- the disconnect


def test_disconnect_removes_the_row(integrations_client, db_session):
    _connect(db_session, "payroll", "gusto", api_token="t", company_id="c1")
    assert integrations_client.delete("/api/integrations/payroll").status_code == 204
    assert _row(db_session, "payroll") is None
    assert db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "integration_disconnected")
    ).scalar_one().resource_id == "payroll"


def test_disconnect_is_a_noop_on_an_absent_row(integrations_client):
    """Matches `checklist_api.undismiss`: a repeat DELETE from a second browser
    tab is not an error, and answering 404 would make absence observable."""
    assert integrations_client.delete("/api/integrations/payroll").status_code == 204
    assert integrations_client.delete("/api/integrations/payroll").status_code == 204


def test_disconnect_refuses_an_unknown_integration(integrations_client):
    assert integrations_client.delete("/api/integrations/telepathy").status_code == 404


# ------------------------------------------------------- the authorization


def test_a_non_admin_cannot_connect(integrations_client_gm):
    resp = integrations_client_gm.put(
        "/api/integrations/demand_feed",
        json={"provider": "tripleseat", "api_key": "k"},
    )
    assert resp.status_code == 403


def test_a_non_admin_cannot_disconnect(integrations_client_gm):
    assert integrations_client_gm.delete("/api/integrations/payroll").status_code == 403


def test_authorization_answers_before_every_other_refusal(
    integrations_client_gm, verifier_spy
):
    """REFUSAL ORDERING. A `Depends`-resolved gate runs before the handler, so
    authorization must BE that gate — this branch has already shipped one bug
    where a dependency-resolved integration refusal outran the 403 and turned
    an out-of-scope caller's rejection into a disclosure.

    Two shapes, both of which would otherwise leak:
      * an unknown integration must be 403, not 404 — the 404 would confirm
        which integration keys this deployment serves;
      * an unauthorized caller must never reach `verify_integration`, or a
        403'd request would still have made a live outbound call with
        attacker-supplied credentials.
    """
    assert integrations_client_gm.put(
        "/api/integrations/telepathy", json={"provider": "gusto", "api_token": "t"},
    ).status_code == 403
    assert integrations_client_gm.delete(
        "/api/integrations/telepathy"
    ).status_code == 403
    assert integrations_client_gm.put(
        "/api/integrations/payroll",
        json={"provider": "gusto", "api_token": "t", "company_id": "c"},
    ).status_code == 403
    assert verifier_spy.calls == []


# ------------------------------------------- verify_credentials (the seam)


def test_the_default_verifier_is_the_real_one(db_engine, tmp_path):
    """Everything above injects the seam, so nothing above would notice if
    `create_app` defaulted it to a no-op — and D-OH17.8 would be false in
    production while the suite stayed green."""
    verifier, _ = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    assert app.state.verify_integration is integrations.verify_credentials


@pytest.fixture
def mocked_providers(monkeypatch) -> None:
    """The REAL adapters over the in-process provider mocks.

    Subclasses that inject only the transport, so `verify_credentials` builds
    and calls the genuine adapter code — a monkeypatched fake adapter would
    prove only that this test's fake was called. The base URLs stay whatever
    `Settings` says; the ASGI transport makes the host irrelevant.
    """
    class _Gusto(GustoAdapter):
        def __init__(self, **kw: str) -> None:
            super().__init__(**kw, transport=SyncASGITransport(create_mock_gusto()))

    class _Adp(AdpAdapter):
        def __init__(self, **kw: str) -> None:
            super().__init__(**kw, transport=SyncASGITransport(create_mock_adp()))

    class _Delphi(DelphiAdapter):
        def __init__(self, **kw: str) -> None:
            super().__init__(**kw, transport=SyncASGITransport(create_mock_delphi()))

    class _Tripleseat(TripleseatAdapter):
        def __init__(self, **kw: str) -> None:
            super().__init__(**kw, transport=SyncASGITransport(create_mock_tripleseat()))

    monkeypatch.setattr(integrations, "GustoAdapter", _Gusto)
    monkeypatch.setattr(integrations, "AdpAdapter", _Adp)
    monkeypatch.setattr(integrations, "DelphiAdapter", _Delphi)
    monkeypatch.setattr(integrations, "TripleseatAdapter", _Tripleseat)


def test_verify_credentials_passes_a_good_credential(mocked_providers):
    verify_credentials("payroll", "gusto",
                       {"api_token": "mock", "company_id": "mock"}, None)
    verify_credentials("payroll", "adp",
                       {"client_id": "mock", "client_secret": "mock"}, None)
    verify_credentials("demand_feed", "delphi",
                       {"subscription_key": "mock"}, DELPHI_HOTEL_REF)
    verify_credentials("demand_feed", "tripleseat",
                       {"api_key": "mock"}, str(TRIPLESEAT_LOCATION_ID))


def test_verify_credentials_raises_the_adapters_own_error(mocked_providers):
    """The router narrows on these exact types, so a 422 is a provider
    refusal and a genuine bug still surfaces as a 500."""
    with pytest.raises(ProviderError):
        verify_credentials("payroll", "gusto",
                           {"api_token": "wrong", "company_id": "mock"}, None)
    with pytest.raises(ProviderError):
        verify_credentials("payroll", "adp",
                           {"client_id": "mock", "client_secret": "wrong"}, None)
    with pytest.raises(CrmFeedError):
        verify_credentials("demand_feed", "delphi",
                           {"subscription_key": "wrong"}, DELPHI_HOTEL_REF)
    with pytest.raises(CrmFeedError):
        verify_credentials("demand_feed", "tripleseat",
                           {"api_key": "wrong"}, str(TRIPLESEAT_LOCATION_ID))


def test_verify_credentials_dispatches_on_the_provider_not_the_fields(
    mocked_providers,
):
    """A Gusto credential aimed at ADP must reach the ADP adapter and fail
    there, never be re-routed by which field names happen to be present:
    field-name inference picks the wrong adapter the first time two providers
    share a name, and it would send one provider's secret to the other."""
    with pytest.raises(ProviderError, match="adp"):
        verify_credentials("payroll", "adp",
                           {"client_id": "mock", "client_secret": "wrong"}, None)


def test_verify_credentials_refuses_a_crm_workspace_with_no_crm_ref(mocked_providers):
    """A NAMED refusal, not an unverified credential silently stored: with no
    property carrying a crm_ref there is nothing to verify against, and
    storing anyway would let the checklist call it done (ADR-010)."""
    with pytest.raises(CannotVerify, match="crm_ref"):
        verify_credentials("demand_feed", "delphi", {"subscription_key": "mock"}, None)


def test_verify_credentials_refuses_qbo_rather_than_passing_it(mocked_providers):
    """QBO is proven by COMPLETING the OAuth grant (Task 11), not by pasting a
    refresh token — and a paste could not be verified anyway, because Intuit
    rotates the token on every grant, so "checking" it would spend it and
    leave the stored copy dead.

    The dispatch must therefore REFUSE, never fall off the end returning None.
    A silent fall-through is a verification that always passes, which is
    D-OH17.8 inverted for the one integration whose credential rotates."""
    with pytest.raises(CannotVerify):
        verify_credentials("accounting", "qbo",
                           {"realm_id": "r", "refresh_token": "t"}, None)


def test_accounting_cannot_be_connected_by_pasting_a_token(
    db_engine, db_session, unconnected_org, tmp_path
):
    """The router half of the rule above, with the REAL verifier wired in —
    the injected spy would happily let a pasted refresh token through, so the
    refusal has to be pinned against the seam that ships."""
    verifier, mint = make_authkit()
    client = _authenticated(
        _client(db_engine, tmp_path, verifier, integrations.verify_credentials),
        mint, db_session, "org_admin", "int-admin",
    )
    resp = client.put(
        "/api/integrations/accounting",
        json={"provider": "qbo", "realm_id": "r", "refresh_token": "t"},
    )
    assert resp.status_code == 422
    assert _row(db_session, "accounting") is None


def test_connect_refuses_a_crm_credential_when_no_property_has_a_crm_ref(
    db_engine, db_session, unconnected_org, tmp_path
):
    """End to end through the real seam, and no HTTP is involved: the refusal
    fires before any adapter is built."""
    verifier, mint = make_authkit()
    client = _authenticated(
        _client(db_engine, tmp_path, verifier, integrations.verify_credentials),
        mint, db_session, "org_admin", "int-admin",
    )
    resp = client.put(
        "/api/integrations/demand_feed",
        json={"provider": "delphi", "subscription_key": "mock"},
    )
    assert resp.status_code == 422
    assert "crm_ref" in resp.json()["detail"]
    assert _row(db_session, "demand_feed") is None


# --------------------------------------------------------- the provider specs


def test_the_read_serves_the_provider_specs(integrations_client):
    """Derived from PROVIDERS, never a hand-written list: a sixth provider
    needs no edit here, and a frontend copy of this data would have nothing
    checking it (see the design doc, section 3)."""
    body = integrations_client.get("/api/integrations").json()
    served = {
        item["integration"]: {p["provider"] for p in item["providers"]}
        for item in body["items"]
    }
    expected: dict[str, set[str]] = {}
    for spec in integrations.PROVIDERS:
        expected.setdefault(spec.integration, set()).add(spec.provider)
    assert served == expected


def test_each_served_field_is_flagged_secret_exactly_as_the_spec_says(
    integrations_client,
):
    body = integrations_client.get("/api/integrations").json()
    by_pair = {
        (item["integration"], p["provider"]): p
        for item in body["items"]
        for p in item["providers"]
    }
    for spec in integrations.PROVIDERS:
        served = by_pair[(spec.integration, spec.provider)]
        assert served["oauth"] is spec.oauth
        assert served["label"] == integrations.product_name(spec.provider)
        secret = {f["name"] for f in served["fields"] if f["secret"]}
        plain = {f["name"] for f in served["fields"] if not f["secret"]}
        assert secret == set(spec.secret_fields)
        assert plain == set(spec.plain_fields)
        # Each field's served label is checked against the accessor, not
        # hard-coded here — a hand-written second copy is exactly the drift
        # `field_label` (usali/integrations.py) exists to prevent, and
        # `test_every_provider_field_has_an_operator_facing_label` is what
        # keeps that accessor itself honest.
        for served_field in served["fields"]:
            assert served_field["label"] == integrations.field_label(served_field["name"])
