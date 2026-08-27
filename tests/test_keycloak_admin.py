import json

import httpx
import pytest

from usali.keycloak_admin import (
    InMemoryKeycloakAdmin,
    KeycloakAdminClient,
    KeycloakAdminError,
)


def _mock_transport() -> httpx.MockTransport:
    """Scripts the admin REST calls create_user + disable_user make."""
    state: dict[str, object] = {"created": None, "roles_mapped": None, "disabled": []}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "admin-tok", "expires_in": 60})
        if p.endswith("/admin/realms/usali/users") and request.method == "POST":
            state["created"] = json.loads(request.content)
            return httpx.Response(
                201, headers={"Location": "http://kc/admin/realms/usali/users/new-sub-1"}
            )
        if p.endswith("/admin/realms/usali/roles") and request.method == "GET":
            return httpx.Response(200, json=[{"id": "r1", "name": "property_gm"}])
        if "/role-mappings/realm" in p and request.method == "POST":
            state["roles_mapped"] = json.loads(request.content)
            return httpx.Response(204)
        if p.endswith("/admin/realms/usali/users/new-sub-1") and request.method == "PUT":
            state["disabled"].append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404, text=f"unexpected {request.method} {p}")

    tr = httpx.MockTransport(handler)
    tr.state = state  # type: ignore[attr-defined]
    return tr


def _client() -> tuple[KeycloakAdminClient, httpx.MockTransport]:
    tr = _mock_transport()
    c = KeycloakAdminClient(
        base_url="http://kc", realm="usali",
        client_id="usali-admin", client_secret="s", transport=tr,
    )
    return c, tr


def test_create_user_returns_subject_and_maps_roles():
    c, tr = _client()
    sub = c.create_user(
        username="gm1", email="gm1@x.com", full_name="Gina Manager",
        realm_roles=["property_gm"],
    )
    assert sub == "new-sub-1"
    assert tr.state["created"]["username"] == "gm1"  # type: ignore[index]
    assert tr.state["created"]["email"] == "gm1@x.com"  # type: ignore[index]
    assert tr.state["roles_mapped"] == [{"id": "r1", "name": "property_gm"}]  # type: ignore[index]


def test_assign_realm_roles_maps_onto_an_existing_user():
    """The adopt-path re-map (Issue 2): assign_realm_roles GETs the realm roles
    and POSTs the mapping for an already-existing subject."""
    c, tr = _client()
    c.assign_realm_roles("new-sub-1", ["property_gm"])
    assert tr.state["roles_mapped"] == [{"id": "r1", "name": "property_gm"}]  # type: ignore[index]


def test_disable_user_puts_enabled_false():
    c, tr = _client()
    c.disable_user("new-sub-1")
    assert tr.state["disabled"] == [{"enabled": False}]  # type: ignore[index]


def test_create_user_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(409, text="user exists")

    c = KeycloakAdminClient(
        base_url="http://kc", realm="usali", client_id="a", client_secret="s",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(KeycloakAdminError):
        c.create_user(username="dup", email="d@x.com", full_name="D", realm_roles=[])


def test_in_memory_fake_create_and_disable():
    fake = InMemoryKeycloakAdmin()
    sub = fake.create_user(username="u", email="u@x.com", full_name="U", realm_roles=["accountant"])
    assert sub in fake.users
    assert fake.users[sub]["enabled"] is True
    assert fake.users[sub]["realm_roles"] == ["accountant"]
    fake.disable_user(sub)
    assert fake.users[sub]["enabled"] is False


def test_fake_assign_realm_roles_unions_and_guards_unknown():
    fake = InMemoryKeycloakAdmin()
    sub = fake.create_user(username="u", email="u@x.com", full_name="U", realm_roles=[])
    fake.assign_realm_roles(sub, ["org_admin"])
    assert fake.users[sub]["realm_roles"] == ["org_admin"]
    fake.assign_realm_roles(sub, ["org_admin"])  # idempotent union — no duplicate
    assert fake.users[sub]["realm_roles"] == ["org_admin"]
    with pytest.raises(KeycloakAdminError, match="unknown user"):
        fake.assign_realm_roles("no-such-sub", ["org_admin"])


# ---------------------------------------------------------------- KC 26 orgs


def _org_transport() -> httpx.MockTransport:
    """Scripts the admin REST calls the org ops make: list, create, add member."""
    state: dict[str, object] = {"orgs": [], "created": None, "members": []}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "admin-tok", "expires_in": 60})
        if p.endswith("/admin/realms/usali/organizations") and request.method == "GET":
            return httpx.Response(200, json=state["orgs"])  # type: ignore[arg-type]
        if p.endswith("/admin/realms/usali/organizations") and request.method == "POST":
            state["created"] = json.loads(request.content)
            return httpx.Response(
                201,
                headers={"Location": "http://kc/admin/realms/usali/organizations/org-77"},
            )
        if "/organizations/org-77/members" in p and request.method == "POST":
            state["members"].append(json.loads(request.content))  # type: ignore[attr-defined]
            return httpx.Response(201)
        return httpx.Response(404, text=f"unexpected {request.method} {p}")

    tr = httpx.MockTransport(handler)
    tr.state = state  # type: ignore[attr-defined]
    return tr


def test_create_organization_posts_name_alias_and_returns_id():
    tr = _org_transport()
    c = KeycloakAdminClient(
        base_url="http://kc", realm="usali", client_id="a", client_secret="s", transport=tr,
    )
    org_id = c.create_organization(name="Rival Group", alias="rival-group")
    assert org_id == "org-77"
    assert tr.state["created"]["name"] == "Rival Group"  # type: ignore[index]
    assert tr.state["created"]["alias"] == "rival-group"  # type: ignore[index]


def test_find_organization_by_alias_matches_exactly():
    tr = _org_transport()
    tr.state["orgs"] = [  # type: ignore[index]
        {"id": "org-1", "alias": "pilot-hotel-group"},
        {"id": "org-77", "alias": "rival-group"},
    ]
    c = KeycloakAdminClient(
        base_url="http://kc", realm="usali", client_id="a", client_secret="s", transport=tr,
    )
    assert c.find_organization_by_alias("rival-group") == "org-77"
    assert c.find_organization_by_alias("no-such-alias") is None


def test_add_member_posts_subject_and_swallows_409():
    tr = _org_transport()
    c = KeycloakAdminClient(
        base_url="http://kc", realm="usali", client_id="a", client_secret="s", transport=tr,
    )
    c.add_member("org-77", "sub-9")
    assert tr.state["members"] == ["sub-9"]  # type: ignore[index]

    # A 409 (already a member) is idempotent, not an error.
    def conflict(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 60})
        return httpx.Response(409, text="already a member")

    c2 = KeycloakAdminClient(
        base_url="http://kc", realm="usali", client_id="a", client_secret="s",
        transport=httpx.MockTransport(conflict),
    )
    c2.add_member("org-77", "sub-9")  # must NOT raise


def test_fake_create_organization_refuses_duplicate_alias():
    """The fake must refuse a duplicate alias exactly as real Keycloak 409s —
    a fake more permissive than prod is how the OIDC audience bug survived."""
    fake = InMemoryKeycloakAdmin()
    org_id = fake.create_organization(name="Rival", alias="rival-group")
    assert fake.find_organization_by_alias("rival-group") == org_id
    with pytest.raises(KeycloakAdminError, match="duplicate alias"):
        fake.create_organization(name="Rival Again", alias="rival-group")


def test_fake_add_member_is_idempotent():
    fake = InMemoryKeycloakAdmin()
    org_id = fake.create_organization(name="Rival", alias="rival-group")
    fake.add_member(org_id, "sub-1")
    fake.add_member(org_id, "sub-1")  # idempotent
    assert fake.organizations[org_id]["members"] == {"sub-1"}
    with pytest.raises(KeycloakAdminError, match="unknown organization"):
        fake.add_member("no-such-org", "sub-1")


def test_read_timeout_absorbs_a_keycloak_cold_start():
    """The read timeout must outlast a scale-to-zero Keycloak cold start.

    Measured on the demo 2026-08-27: a request that woke `usali-auth` from
    minScale=0 reached the startup probe ~18s later (11.8s of Quarkus JVM boot,
    behind a cloud-sql-proxy sidecar that starts first). Cloud Run queues the
    caller for that whole window, so a flat `timeout=10` made EVERY signup that
    landed on a cold instance fail in `_token()` — the first stranger after any
    quiet spell, which is the worst possible way to distribute a failure.

    Connect stays short on purpose: the Google front end accepts the connection
    immediately even while the container boots, so a slow CONNECT is a real
    fault and should still fail fast.
    """
    c, _ = _client()
    timeout = c._http.timeout
    assert timeout.read is not None and timeout.read >= 45, (
        "read timeout must clear the ~18s cold start with margin"
    )
    assert timeout.connect is not None and timeout.connect <= 15
