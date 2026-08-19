"""set_password on the KeycloakAdmin seam: clears UPDATE_PASSWORD, sets
emailVerified; the fake records it and raises on an unknown id; provision_tenant
threads a password through."""

import httpx
import pytest

from usali.keycloak_admin import (
    InMemoryKeycloakAdmin,
    KeycloakAdminClient,
    KeycloakAdminError,
)
from usali.provisioning import provision_tenant
from usali.mapping.property_registry import ensure_default_org


def test_fake_set_password_records_and_clears_required_action():
    kc = InMemoryKeycloakAdmin()
    sub = kc.create_user(
        username="owner", email="owner@example.test", full_name="Ow Ner",
        realm_roles=["org_admin"],
    )
    kc.set_password(sub, "s3cret-passphrase")
    user = kc.users[sub]
    assert user["password"] == "s3cret-passphrase"
    assert user["required_actions"] == []
    assert user["email_verified"] is True


def test_fake_set_password_unknown_id_raises():
    kc = InMemoryKeycloakAdmin()
    with pytest.raises(KeycloakAdminError, match="unknown user"):
        kc.set_password("nope", "x")


def test_real_client_set_password_calls_reset_and_clears_actions():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(204)

    client = KeycloakAdminClient(
        base_url="http://kc.local", realm="usali",
        client_id="usali-admin", client_secret="s",
        transport=httpx.MockTransport(handler),
    )
    client.set_password("sub-123", "pw")
    paths = [p for m, p in seen]
    assert "/admin/realms/usali/users/sub-123/reset-password" in paths
    assert "/admin/realms/usali/users/sub-123" in paths


def test_provision_tenant_sets_the_admin_password(db_session):
    ensure_default_org(db_session)
    db_session.commit()
    kc = InMemoryKeycloakAdmin()
    result = provision_tenant(
        db_session, kc,
        org_name="Pw Org", org_alias="pw-org",
        admin_username="pw-admin", admin_email="pw@example.test",
        admin_full_name="Pw Admin", password="chosen-pw",
    )
    db_session.commit()
    assert kc.users[result.admin_subject]["password"] == "chosen-pw"
    assert kc.users[result.admin_subject]["email_verified"] is True
