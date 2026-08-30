"""The QBO OAuth pair — `authorize` and `callback` (OH-17 Task 11).

This is the one route in the system whose TENANT IDENTITY comes from a signed
parameter rather than from a validated token. The Intuit callback arrives as a
top-level browser navigation with no bearer token and no active-org header, so
`require_operator` and `require_active_org` cannot run on it (D-OH17.11);
`state` is the only carrier of "which tenant is this for". If it is forgeable,
an attacker binds their own QuickBooks company to someone else's workspace.

Every test here exists to keep one of three properties honest:

1. **`state` is unforgeable.** `test_a_tampered_org_id_is_refused` and
   `test_a_forged_state_cannot_bind_a_credential_to_another_tenants_org` are
   the assertions this task exists for. The second is at the DATABASE, not
   through the router that refused: an HTTP-layer-only assertion would pass on
   a write that landed and was then rolled back.

2. **One refusal for every failure mode.** Forged, expired, malformed and
   MISSING must be byte-identical to the caller, or the refusal is an oracle
   about other tenants' in-flight grants. Pinned by comparing the whole
   response, not just the status.

3. **Authority comes from the signature, never from the request.** The org is
   the one inside `state` — not a query parameter
   (`test_the_org_comes_from_the_signature_not_a_query_parameter`) — and the
   redirect URI is built from configuration, not from the Host header
   (`test_the_redirect_uri_ignores_the_host_header`), which is
   attacker-controlled behind a proxy.

Fixture note (D-OH17.15): `unconnected_org`, never `founding_org`. The latter
runs the seed bridge, which unconditionally plants org 1's payroll and
accounting rows from process env — so "accounting is not connected" would be
false before the test began.
"""

import inspect
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.authkit import make_authkit
from tests.grants import grant_role
from usali.config import get_settings
from usali.crypto import oauth_state_key
from usali.db import make_session_factory
from usali.integrations_api import qbo_redirect_uri, sign_state, verify_state
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import AuditEvent, OrgIntegrationCredential
from usali.qbo_client import QboError, SyncASGITransport, exchange_authorization_code
from usali.qbo_mock import create_mock_qbo
from usali.server import _exchange_qbo_code_from_settings, create_app

_CALLBACK = "/api/integrations/accounting/callback"
_AUTHORIZE = "/api/integrations/accounting/authorize"


class _Exchange:
    """The injected code-exchange seam, recording what it was asked.

    `error` is settable AFTER the client is built, because the router reads
    the seam at call time — one spy serves both the granting and the refusing
    world (the `_Verifier` shape in `test_integrations_api.py`)."""

    def __init__(self, refresh_token: str = "rotated-refresh-token") -> None:
        self.codes: list[str] = []
        self.refresh_token = refresh_token
        self.error: Exception | None = None

    def __call__(self, code: str) -> str:
        self.codes.append(code)
        if self.error is not None:
            raise self.error
        return self.refresh_token


@pytest.fixture
def exchange_spy() -> _Exchange:
    return _Exchange()


def _app(db_engine, tmp_path, verifier, exchange):
    return create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        exchange_qbo_code=exchange,
    )


def _client(app) -> TestClient:
    # follow_redirects=False on purpose: the success path is a redirect to the
    # SPA route `/integrations`, which does not exist in a test app with no
    # built frontend — following it would turn every green assertion into a
    # 404 and hide the Location header this suite reads.
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def oauth_client(db_engine, db_session, unconnected_org, tmp_path, exchange_spy):
    """UNAUTHENTICATED on purpose. The callback carries no bearer token in
    production, so a client that quietly held one would prove nothing about
    the route that actually ships."""
    verifier, _ = make_authkit()
    return _client(_app(db_engine, tmp_path, verifier, exchange_spy))


@pytest.fixture
def admin_client(db_engine, db_session, unconnected_org, tmp_path, exchange_spy):
    """An org_admin of org 1 — for `authorize`, which IS gated."""
    verifier, mint = make_authkit()
    client = _client(_app(db_engine, tmp_path, verifier, exchange_spy))
    grant_role(db_session, "org_admin", sub="int-admin", org_id=1)
    client.headers["Authorization"] = f"Bearer {mint(roles=['org_admin'], sub='int-admin')}"
    return client


@pytest.fixture
def gm_client(db_engine, db_session, unconnected_org, tmp_path, exchange_spy):
    """A property_gm — an ORG-WIDE grant, so the strongest non-admin there is.
    Starting a tenant's QuickBooks grant is not a GM's call."""
    verifier, mint = make_authkit()
    client = _client(_app(db_engine, tmp_path, verifier, exchange_spy))
    grant_role(db_session, "property_gm", sub="int-gm", org_id=1)
    client.headers["Authorization"] = f"Bearer {mint(roles=['property_gm'], sub='int-gm')}"
    return client


@pytest.fixture
def org2_id(two_tenant_world) -> int:
    return int(two_tenant_world.org2_id)


@pytest.fixture
def two_org_oauth_client(db_engine, db_session, two_tenant_world, tmp_path, exchange_spy):
    """The unauthenticated callback client over the SHARED two-org world.

    On the SUPERUSER engine (`db_engine`) deliberately, not the RLS-bound app
    role: RLS would REFUSE a wrong-org write, so a cross-tenant bug would be
    masked by the wall and this suite would pass for the wrong reason. Here
    the org binding is proven by the router's own code, which is the property
    D-OH17.11 is about — the walls are the second line, pinned elsewhere.

    `two_tenant_world` builds on `founding_org`, whose D-OH17.15 seed bridge
    unconditionally plants org 1's credential rows, so "org 1 was not written"
    would be false before the test began. Cleared here, SCOPED to the two orgs
    (an unscoped DELETE on a superuser session is confined by nothing)."""
    db_session.execute(
        text("DELETE FROM org_integration_credential WHERE org_id IN (:one, :two)"),
        {"one": 1, "two": two_tenant_world.org2_id},
    )
    db_session.commit()
    verifier, _ = make_authkit()
    return _client(_app(db_engine, tmp_path, verifier, exchange_spy))


def _rows(db_session) -> list[OrgIntegrationCredential]:
    db_session.expire_all()  # the endpoint committed on ANOTHER connection
    return list(db_session.execute(
        select(OrgIntegrationCredential).where(
            OrgIntegrationCredential.integration == "accounting"
        ).order_by(OrgIntegrationCredential.org_id)
    ).scalars())


# ------------------------------------------------------------- the signature


def test_a_valid_state_round_trips():
    assert verify_state(sign_state(org_id=7, subject="sub")) == (7, "sub")


def test_a_forged_state_is_refused():
    """The shape an attacker can actually produce: they know the format and
    the org they want, and they do not know the key."""
    assert verify_state("1:sub:9999999999:deadbeef") is None


def test_a_tampered_org_id_is_refused():
    """THE signature assertion. A real state, minted for the attacker's own
    org, with the org id rewritten to the victim's — the exact edit a
    cross-tenant credential injection needs, and the one an unsigned or
    weakly-signed state would wave through."""
    honest = sign_state(org_id=2, subject="attacker")
    tampered = "1" + honest[1:]
    assert honest.startswith("2:")          # the edit really is one character
    assert verify_state(honest) == (2, "attacker")
    assert verify_state(tampered) is None


def test_a_tampered_subject_is_refused():
    honest = sign_state(org_id=7, subject="attacker")
    assert verify_state(honest.replace("attacker", "victim--")) is None


def test_a_tampered_expiry_is_refused():
    """Extending the TTL is the other half of the forgery: an expiry the
    holder can edit is no expiry at all."""
    honest = sign_state(org_id=7, subject="sub", now=time.time() - 3600)
    org, subject, expiry, mac = honest.split(":")
    assert verify_state(f"{org}:{subject}:{int(expiry) + 100000}:{mac}") is None


def test_an_expired_state_is_refused():
    assert verify_state(sign_state(org_id=1, subject="sub", now=time.time() - 3600)) is None


def test_a_state_signed_now_is_still_live():
    """The direction that kills the "expire everything" mutant: an always-None
    verify would pass every refusal test above and break the whole feature."""
    assert verify_state(sign_state(org_id=1, subject="sub")) == (1, "sub")


@pytest.mark.parametrize("state", [
    "",                       # missing, normalised to empty by the router
    "not-a-state",
    "1:sub:9999999999",       # three parts: no MAC at all
    "1:sub:9999999999:",      # empty MAC
    "1:sub:notanumber:" + "0" * 64,
    ":::",
    "1:sub:9999999999:" + "z" * 64,   # non-hex MAC
    # NON-ASCII. `hmac.compare_digest` raises TypeError on a str holding one,
    # so a MAC compared as `str` turns a two-byte query parameter into a 500 —
    # a refusal shape no other failure mode produces, and a crash on
    # unauthenticated input. The comparison is done on BYTES for this reason.
    "1:sub:9999999999:é",
    "1:sub:9999999999:" + "🙂" * 32,
    "1:sübject:9999999999:" + "de" * 32,
])
def test_a_malformed_state_is_refused(state):
    """Every wrong shape returns None rather than raising: a ValueError (or a
    TypeError) here would be a 500 whose traceback distinguishes the failure
    modes the single refusal exists to blur."""
    assert verify_state(state) is None


def test_the_signature_is_compared_in_constant_time():
    """`==` and `hmac.compare_digest` agree on every INPUT — they differ only
    in how long they take to disagree, so no behavioural test can tell them
    apart. This one reads the source instead, because a timing-variable MAC
    comparison is the textbook forgery oracle and something has to hold it.

    Do not "simplify" `verify_state` to `if mac != expected`."""
    source = inspect.getsource(verify_state)
    assert "hmac.compare_digest(" in source
    # ...and no plain equality on the MAC slipped in beside it.
    assert not re.search(r"\bmac\s*[!=]=", source)
    assert not re.search(r"[!=]=\s*expected\b", source)


def test_the_state_key_is_derived_and_is_not_the_master_key():
    """HKDF from `field_encryption_key` under a fixed domain label (the
    `_photo_key` precedent), so OH-17 introduces no new deployment secret —
    but it must NOT be the master key itself, or a state forgery and a
    field-decryption become the same compromise."""
    from usali.crypto import _key, _photo_key

    key = oauth_state_key()
    assert len(key) == 32
    assert key == oauth_state_key()      # deterministic across calls
    assert key != _key()                 # domain-separated from the master
    assert key != _photo_key(1)          # ...and from org 1's photo key
    assert key != _photo_key(2)


# --------------------------------------------------------------- authorize


def _consent_params(client: TestClient, **kwargs) -> dict[str, str]:
    resp = client.get(_AUTHORIZE, **kwargs)
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith(get_settings().qbo_authorize_url + "?")
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def test_authorize_returns_a_consent_url_bound_to_the_active_org(admin_client):
    """Returns the URL rather than 302-ing: the SPA navigates the top-level
    window itself, so the fetch seam never has to follow a cross-origin
    redirect."""
    params = _consent_params(admin_client)
    assert params["client_id"] == get_settings().qbo_client_id
    assert params["response_type"] == "code"
    assert params["scope"] == "com.intuit.quickbooks.accounting"
    assert verify_state(params["state"]) == (1, "int-admin")


def test_the_redirect_uri_ignores_the_host_header(admin_client):
    """Built from `public_base_url`, never from the request. Behind a proxy the
    Host header is attacker-controlled, and a redirect_uri derived from it
    would send the tenant's authorization code to the attacker's domain."""
    params = _consent_params(admin_client, headers={"Host": "evil.example"})
    assert params["redirect_uri"] == (
        f"{get_settings().public_base_url}{_CALLBACK}"
    )
    assert "evil.example" not in params["redirect_uri"]


def test_the_authorize_and_exchange_redirect_uris_are_the_same_string(monkeypatch):
    """Intuit compares `redirect_uri` byte-for-byte between the consent
    request and the code exchange; a mismatch is `invalid_grant` at grant
    time, long after the URL looked fine. One expression, both call sites."""
    seen: dict[str, str] = {}

    def _capture(code, *, base_url, client_id, client_secret, redirect_uri,
                 transport=None):
        seen["redirect_uri"] = redirect_uri
        return "refresh"

    monkeypatch.setattr("usali.server.exchange_authorization_code", _capture)
    _exchange_qbo_code_from_settings("code-1")
    assert seen["redirect_uri"] == qbo_redirect_uri(get_settings())


def test_authorize_writes_an_audit_event(admin_client, db_session):
    admin_client.get(_AUTHORIZE)
    event = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "integration_authorize_started")
    ).scalar_one()
    assert event.resource_id == "accounting"
    assert event.actor_subject == "int-admin"
    assert event.org_id == 1


def test_a_non_admin_cannot_start_the_grant(gm_client):
    assert gm_client.get(_AUTHORIZE).status_code == 403


def test_an_anonymous_caller_cannot_start_the_grant(oauth_client):
    """`authorize` is gated even though `callback` is not: the callback's
    authority is a signature it verifies, and `authorize` is what MINTS
    those signatures. An ungated authorize hands anyone a valid state."""
    assert oauth_client.get(_AUTHORIZE).status_code == 401


# ---------------------------------------------------------------- callback


def test_the_callback_writes_under_the_org_named_in_state(oauth_client, db_session):
    """D-OH17.11: no bearer token, no active-org header — `state` is the only
    carrier of tenant identity, and the row must bind to the org INSIDE it."""
    resp = oauth_client.get(
        _CALLBACK,
        params={"code": "good", "realmId": "r1",
                "state": sign_state(org_id=1, subject="admin-sub")},
    )
    assert resp.status_code == 307
    assert resp.headers["location"] == "/integrations?connected=accounting"
    row = _rows(db_session)
    assert len(row) == 1
    assert (row[0].org_id, row[0].integration, row[0].provider) == (1, "accounting", "qbo")
    assert row[0].realm_id == "r1"
    assert row[0].refresh_token == "rotated-refresh-token"
    assert row[0].connected_by == "admin-sub"


def test_the_callback_needs_no_token_and_spends_the_code_exactly_once(
    oauth_client, exchange_spy
):
    oauth_client.get(_CALLBACK, params={
        "code": "code-abc", "realmId": "r1",
        "state": sign_state(org_id=1, subject="s"),
    })
    assert exchange_spy.codes == ["code-abc"]
    assert "Authorization" not in oauth_client.headers


def test_a_forged_state_cannot_bind_a_credential_to_another_tenants_org(
    oauth_client, db_session, exchange_spy
):
    """THE assertion this task exists for. The attacker holds their own
    QuickBooks company and a callback URL; what they do not hold is the key.
    A forged state must leave the victim's workspace untouched — and must not
    even reach the token exchange, because a request refused only after
    dialling out has already spent someone's code."""
    forged = f"1:victim-admin:{int(time.time()) + 600}:{'de' * 32}"
    resp = oauth_client.get(_CALLBACK, params={
        "code": "attacker-code", "realmId": "attacker-realm", "state": forged,
    })
    assert resp.status_code == 400
    assert _rows(db_session) == []
    assert exchange_spy.codes == []


def test_an_expired_state_cannot_bind_a_credential(oauth_client, db_session):
    """The TTL is load-bearing, not hygiene: a state with no expiry is a
    permanent bearer credential for writing that org's accounting row, and it
    travels through the operator's browser history and any proxy in between."""
    stale = sign_state(org_id=1, subject="s", now=time.time() - 3600)
    assert oauth_client.get(_CALLBACK, params={
        "code": "good", "realmId": "r1", "state": stale,
    }).status_code == 400
    assert _rows(db_session) == []


def test_every_bad_state_is_refused_the_exact_same_way(oauth_client):
    """Forged, expired, malformed and MISSING are one refusal, byte for byte.
    The difference between them is an oracle about other tenants' in-flight
    grants — and "missing" is the one that slips: declared as a required query
    parameter it would be FastAPI's 422 naming the field, which no other
    failure mode produces."""
    def _refusal(params):
        resp = oauth_client.get(_CALLBACK, params=params)
        return resp.status_code, resp.text

    base = {"code": "good", "realmId": "r1"}
    forged = _refusal({**base, "state": "1:s:9999999999:" + "de" * 32})
    assert forged == (400, '{"detail":"invalid authorization state"}')
    assert _refusal({**base, "state": "garbage"}) == forged
    assert _refusal({**base, "state": ""}) == forged
    assert _refusal(base) == forged                              # missing
    assert _refusal({**base, "state": sign_state(
        org_id=1, subject="s", now=time.time() - 3600)}) == forged   # expired


def test_the_org_comes_from_the_signature_not_a_query_parameter(
    two_org_oauth_client, db_session, org2_id
):
    """A signed state for org 2 arriving with `?org_id=1` must write org 2.
    The mutant is one line — read the org from the request instead of from
    the verified state — and it is a cross-tenant credential injection with
    no forgery required at all."""
    resp = two_org_oauth_client.get(_CALLBACK, params={
        "code": "good", "realmId": "org2-realm", "org_id": "1",
        "state": sign_state(org_id=org2_id, subject="org2-admin"),
    })
    assert resp.status_code == 307
    rows = _rows(db_session)
    assert [(r.org_id, r.realm_id) for r in rows] == [(org2_id, "org2-realm")]


def test_a_state_for_one_org_never_touches_the_others_row(
    two_org_oauth_client, db_session, org2_id
):
    """Org 1 already has a working accounting connection. A grant completed
    for org 2 must add org 2's row and leave org 1's exactly as it was — the
    UPDATE arm of the upsert is where a missing org predicate turns into a
    silent cross-tenant overwrite."""
    # Through the ORM, not raw SQL: `refresh_token` is an `EncryptedString`,
    # so a plaintext INSERT reads back as a base64 error rather than a row.
    db_session.add(OrgIntegrationCredential(
        org_id=1, integration="accounting", provider="qbo",
        realm_id="org1-realm", refresh_token="org1-token",
        connected_by="org1-admin",
    ))
    db_session.commit()

    two_org_oauth_client.get(_CALLBACK, params={
        "code": "good", "realmId": "org2-realm",
        "state": sign_state(org_id=org2_id, subject="org2-admin"),
    })
    rows = {r.org_id: r for r in _rows(db_session)}
    assert set(rows) == {1, org2_id}
    assert rows[1].realm_id == "org1-realm"
    assert rows[1].connected_by == "org1-admin"
    assert rows[org2_id].realm_id == "org2-realm"


def test_a_refused_grant_stores_nothing(oauth_client, db_session, exchange_spy):
    """Intuit's `code` is single-use AT INTUIT, which is what makes the nonce
    store unnecessary (D-OH17.11 as amended): a replayed state carries a spent
    code and the exchange refuses it. That refusal must leave no row — the
    same "verify before persist" rule D-OH17.8 puts on the paste path."""
    exchange_spy.error = QboError(400, "invalid_grant")
    resp = oauth_client.get(_CALLBACK, params={
        "code": "already-spent", "realmId": "r1",
        "state": sign_state(org_id=1, subject="s"),
    })
    assert resp.status_code == 400
    assert _rows(db_session) == []


def test_the_callback_replaces_an_existing_connection_wholly(
    oauth_client, db_session, exchange_spy
):
    """Re-consenting is a full replace, exactly as PUT is: the rotated refresh
    token and the new realm land on the SAME row, and no field of another
    provider survives."""
    for realm, token in (("first", "t1"), ("second", "t2")):
        exchange_spy.refresh_token = token
        assert oauth_client.get(_CALLBACK, params={
            "code": "good", "realmId": realm,
            "state": sign_state(org_id=1, subject="s"),
        }).status_code == 307
    rows = _rows(db_session)
    assert len(rows) == 1
    assert (rows[0].realm_id, rows[0].refresh_token) == ("second", "t2")
    assert rows[0].api_token is None and rows[0].company_id is None
    assert rows[0].client_id is None and rows[0].client_secret is None
    assert rows[0].subscription_key is None and rows[0].api_key is None


def test_the_callback_writes_an_audit_event(oauth_client, db_session):
    oauth_client.get(_CALLBACK, params={
        "code": "good", "realmId": "r1",
        "state": sign_state(org_id=1, subject="admin-sub"),
    })
    event = db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "integration_connected")
    ).scalar_one()
    assert (event.resource_id, event.actor_subject, event.org_id) == (
        "accounting", "admin-sub", 1
    )


def test_no_secret_is_ever_on_the_wire(oauth_client, exchange_spy):
    """The module's headline guarantee applied to the one route that HOLDS a
    freshly minted secret. Greps the whole response — headers included,
    because the answer here is a redirect and a `?token=` on a Location is a
    secret in the operator's browser history and every proxy log between."""
    exchange_spy.refresh_token = "sentinel-refresh-token"
    resp = oauth_client.get(_CALLBACK, params={
        "code": "sentinel-authorization-code", "realmId": "r1",
        "state": sign_state(org_id=1, subject="s"),
    })
    whole = resp.text + "\n" + "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
    assert "sentinel-refresh-token" not in whole
    assert "sentinel-authorization-code" not in whole
    # ...while the redirect IS there, so the assertion above cannot pass by
    # the endpoint having answered nothing at all.
    assert "connected=accounting" in whole


# --------------------------------------------------- the code-exchange seam


def test_the_default_code_exchange_is_the_real_one(db_engine, tmp_path):
    """Everything above injects the seam, so nothing above would notice if
    `create_app` defaulted it to a stub returning a constant — and the whole
    OAuth flow would be false in production with the suite green. The same
    hole `test_the_default_verifier_is_the_real_one` closes for D-OH17.8."""
    verifier, _ = make_authkit()
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    assert app.state.exchange_qbo_code is _exchange_qbo_code_from_settings


def test_the_real_exchange_completes_an_authorization_code_grant():
    """The REAL client function against the in-process mock, so the grant
    shape (Basic auth, `grant_type=authorization_code`, the redirect_uri) is
    proven rather than assumed. A monkeypatched fake would prove only that
    this test's fake was called."""
    transport = SyncASGITransport(create_mock_qbo())
    token = exchange_authorization_code(
        "code-1", base_url="http://qbo.invalid", client_id="mock",
        client_secret="mock", redirect_uri="https://app.example/cb",
        transport=transport,
    )
    assert token and isinstance(token, str)
    assert token != "code-1"


def test_the_real_exchange_raises_qbo_error_on_a_refusal():
    """The router narrows on QboError, so a spent or bogus code has to arrive
    as one — anything else is a 500 on an ordinary, expected refusal."""
    transport = SyncASGITransport(create_mock_qbo())
    with pytest.raises(QboError):
        exchange_authorization_code(
            "", base_url="http://qbo.invalid", client_id="mock",
            client_secret="mock", redirect_uri="https://app.example/cb",
            transport=transport,
        )


# ------------------------------------------------------------- the mounting


def test_the_callback_is_mounted_outside_the_operator_gates(oauth_client):
    """The structural half of D-OH17.11, asserted through the door rather than
    by reading `app.routes` (which this FastAPI defers into opaque
    `_IncludedRouter` wrappers). A callback behind `operator_gates` answers
    401 to every real Intuit navigation — and the tempting fix for THAT is to
    weaken the gates for everything.

    The pair is the assertion. The callback must reach its OWN refusal (400,
    "your state did not verify"), and the two gated surfaces beside it must
    still refuse an anonymous caller — so this cannot pass by the gates having
    been dropped across the board."""
    assert oauth_client.get(_CALLBACK).status_code == 400
    assert oauth_client.get(_AUTHORIZE).status_code == 401
    assert oauth_client.get("/api/integrations").status_code == 401
