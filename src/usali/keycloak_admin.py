"""Keycloak admin-API client for operator provisioning (Pillar A2.3).

`KeycloakAdmin` is the injectable seam (mirrors A1's TokenVerifier / the QBO
client): tests inject `InMemoryKeycloakAdmin`; production uses
`KeycloakAdminClient` (real admin REST via a confidential service-account
client). The real client is unit-tested with `httpx.MockTransport` — no running
Keycloak in the test loop.
"""

from typing import Protocol

import httpx

from usali.config import Settings


class KeycloakAdminError(Exception):
    """An admin-API call failed (non-2xx or missing Location)."""


class KeycloakAdminConflict(KeycloakAdminError):
    """A realm user already exists under this username but is a DIFFERENT person.

    Raised rather than reusing the subject: two people whose emails share a
    local-part (dana@hotel-a.com, dana@hotel-b.com) would otherwise collapse into
    one realm account, and the second person would silently inherit the first
    one's identity and roles.
    """


class KeycloakAdmin(Protocol):
    def find_user_by_username(self, username: str) -> tuple[str, str] | None:
        """Return (subject_id, email) for an existing user, or None.

        Provisioning MUST consult this first. `create_user` is a non-transactional
        external side effect: if it succeeds and the caller's DB transaction later
        rolls back, the realm account survives with no employee row and no
        role_assignment. Since L4 such an orphan holds no org authority (role
        authority is the org-scoped role_assignment grants, not realm roles),
        but it still passes the coarse operator gate and no DB-driven view can
        see it, so terminate_employee can never reach it — an account to avoid
        creating, not merely a harmless leftover.
        """
        ...

    def create_user(
        self, *, username: str, email: str, full_name: str, realm_roles: list[str]
    ) -> str:
        """Create an enabled user (UPDATE_PASSWORD required action) + map realm
        roles. Returns the new Keycloak subject id."""
        ...

    def assign_realm_roles(self, subject_id: str, realm_roles: list[str]) -> None:
        """(Re-)map realm roles onto an EXISTING user (idempotent).

        Provisioning/onboarding call this on the ADOPT path (a user found by
        `find_user_by_username`, not created): create_user maps roles as its
        last step, so a prior run that created the user but died before the
        mapping would otherwise leave the admin without even the coarse
        operator role. Re-mapping on adoption closes that create-then-map gap;
        Keycloak treats an already-assigned role as a no-op."""
        ...

    def disable_user(self, subject_id: str) -> None:
        """Disable (soft-delete) a user — used on termination."""
        ...

    # -- KC 26 Organizations (Pillar L decision 6, provisioning primitive) --
    # Same find-or-create posture as create_user/find_user_by_username above:
    # provisioning consults the lookup first and creates only if absent, so a
    # re-run adopts the existing org rather than duplicating it.

    def find_organization_by_alias(self, alias: str) -> str | None:
        """Return the KC organization id for the org with this alias, or None.

        `provision_tenant` consults this FIRST (the create_user posture): an
        organization is a non-transactional external side effect, so a re-run
        after a partial failure must adopt the existing org, never mint a
        second one under the same alias."""
        ...

    def create_organization(self, *, name: str, alias: str) -> str:
        """Create a KC 26 Organization; return its id. MUST refuse a duplicate
        alias (real Keycloak 409s) — see :class:`InMemoryKeycloakAdmin` for
        why the fake refuses too."""
        ...

    def add_member(self, org_id: str, subject_id: str) -> None:
        """Add a realm user to a KC organization. IDEMPOTENT: adding a member
        already in the org is a no-op (real Keycloak 409s an existing member;
        the client swallows it), so provisioning can re-run safely."""
        ...


class KeycloakAdminClient:
    """Talks the slice of the Keycloak admin REST API onboarding needs."""

    def __init__(
        self,
        *,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._realm = realm
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(base_url=base_url.rstrip("/"), transport=transport, timeout=10)

    @classmethod
    def from_settings(cls, settings: Settings) -> "KeycloakAdminClient":
        return cls(
            base_url=settings.kc_admin_base_url,
            realm=settings.kc_admin_realm,
            client_id=settings.kc_admin_client_id,
            client_secret=settings.kc_admin_client_secret,
        )

    def _token(self) -> str:
        r = self._http.post(
            f"/realms/{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        if r.status_code != 200:
            raise KeycloakAdminError(f"token grant failed: {r.status_code}")
        token: str = r.json()["access_token"]
        return token

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def find_user_by_username(self, username: str) -> tuple[str, str] | None:
        r = self._http.get(
            f"/admin/realms/{self._realm}/users",
            headers=self._auth(),
            params={"username": username, "exact": "true"},
        )
        if r.status_code != 200:
            raise KeycloakAdminError(f"user lookup failed: {r.status_code}")
        for user in r.json():
            if user.get("username") == username:
                subject_id: str = user["id"]
                return subject_id, str(user.get("email") or "")
        return None

    def create_user(
        self, *, username: str, email: str, full_name: str, realm_roles: list[str]
    ) -> str:
        first, _, last = full_name.partition(" ")
        headers = self._auth()
        r = self._http.post(
            f"/admin/realms/{self._realm}/users",
            headers=headers,
            json={
                "username": username,
                "email": email,
                "firstName": first,
                "lastName": last or first,
                "enabled": True,
                "requiredActions": ["UPDATE_PASSWORD"],
            },
        )
        if r.status_code != 201:
            raise KeycloakAdminError(f"create user failed: {r.status_code} {r.text}")
        location: str = r.headers.get("Location", "")
        subject_id: str = location.rstrip("/").rsplit("/", 1)[-1]
        if not subject_id:
            raise KeycloakAdminError("create user: no Location/subject id returned")
        if realm_roles:
            self._map_realm_roles(subject_id, realm_roles, headers)
        return subject_id

    def assign_realm_roles(self, subject_id: str, realm_roles: list[str]) -> None:
        if realm_roles:
            self._map_realm_roles(subject_id, realm_roles, self._auth())

    def _map_realm_roles(
        self, subject_id: str, realm_roles: list[str], headers: dict[str, str]
    ) -> None:
        available = self._http.get(
            f"/admin/realms/{self._realm}/roles", headers=headers
        ).json()
        by_name = {role["name"]: role for role in available}
        reps = [
            {"id": by_name[name]["id"], "name": name}
            for name in realm_roles
            if name in by_name
        ]
        if reps:
            r = self._http.post(
                f"/admin/realms/{self._realm}/users/{subject_id}/role-mappings/realm",
                headers=headers,
                json=reps,
            )
            if r.status_code not in (204, 201):
                raise KeycloakAdminError(f"role mapping failed: {r.status_code}")

    def disable_user(self, subject_id: str) -> None:
        r = self._http.put(
            f"/admin/realms/{self._realm}/users/{subject_id}",
            headers=self._auth(),
            json={"enabled": False},
        )
        if r.status_code not in (204, 200):
            raise KeycloakAdminError(f"disable user failed: {r.status_code}")

    def find_organization_by_alias(self, alias: str) -> str | None:
        r = self._http.get(
            f"/admin/realms/{self._realm}/organizations",
            headers=self._auth(),
            params={"search": alias},
        )
        if r.status_code != 200:
            raise KeycloakAdminError(f"organization lookup failed: {r.status_code}")
        # `search` is a SUBSTRING match over name/domains and this call is
        # UNPAGED (default page size), so this is a best-effort narrowing, not a
        # guarantee: we still match the `alias` field EXACTLY here, and a true
        # existing org that the search page happened not to return simply falls
        # through to create_organization, which KC 409s on the duplicate alias
        # — fail-closed (a loud error), never a silent second org.
        for org in r.json():
            if org.get("alias") == alias:
                org_id: str = org["id"]
                return org_id
        return None

    def create_organization(self, *, name: str, alias: str) -> str:
        r = self._http.post(
            f"/admin/realms/{self._realm}/organizations",
            headers=self._auth(),
            json={
                "name": name,
                "alias": alias,
                # KC 26 requires at least one domain on an organization; the
                # alias yields a stable, unique placeholder. Provisioning does
                # not use domain-based membership (members are added by id).
                "domains": [{"name": f"{alias}.local", "verified": False}],
            },
        )
        if r.status_code != 201:
            raise KeycloakAdminError(
                f"create organization failed: {r.status_code} {r.text}"
            )
        location: str = r.headers.get("Location", "")
        org_id: str = location.rstrip("/").rsplit("/", 1)[-1]
        if not org_id:
            raise KeycloakAdminError(
                "create organization: no Location/org id returned"
            )
        return org_id

    def add_member(self, org_id: str, subject_id: str) -> None:
        r = self._http.post(
            f"/admin/realms/{self._realm}/organizations/{org_id}/members",
            headers={**self._auth(), "Content-Type": "application/json"},
            # The admin API takes the bare user id as a JSON-string body.
            json=subject_id,
        )
        if r.status_code == 409:
            return  # already a member — idempotent, not an error
        if r.status_code not in (201, 204):
            raise KeycloakAdminError(f"add member failed: {r.status_code}")


class InMemoryKeycloakAdmin:
    """Offline fake for tests/dev. Records users in a dict; deterministic ids."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, object]] = {}
        self._counter = 0
        # org_id -> {"name", "alias", "members": set[str]} (KC 26 orgs).
        self.organizations: dict[str, dict[str, object]] = {}
        self._org_counter = 0

    def find_user_by_username(self, username: str) -> tuple[str, str] | None:
        for subject_id, user in self.users.items():
            if user["username"] == username:
                return subject_id, str(user["email"])
        return None

    def create_user(
        self, *, username: str, email: str, full_name: str, realm_roles: list[str]
    ) -> str:
        # Real Keycloak returns 409 on a duplicate username. The fake MUST refuse
        # too: a double more permissive than production is how the OIDC audience
        # bug survived 700+ green tests and only surfaced on a real browser login.
        if self.find_user_by_username(username) is not None:
            raise KeycloakAdminError(f"create user failed: 409 duplicate username {username!r}")
        self._counter += 1
        subject_id = f"kc-{username}-{self._counter}"
        self.users[subject_id] = {
            "username": username, "email": email, "full_name": full_name,
            "realm_roles": realm_roles, "enabled": True,
        }
        return subject_id

    def assign_realm_roles(self, subject_id: str, realm_roles: list[str]) -> None:
        user = self.users.get(subject_id)
        if user is None:
            raise KeycloakAdminError(f"assign roles failed: unknown user {subject_id!r}")
        current = user["realm_roles"]
        assert isinstance(current, list)
        for role in realm_roles:  # union — Keycloak no-ops an already-mapped role
            if role not in current:
                current.append(role)

    def disable_user(self, subject_id: str) -> None:
        if subject_id in self.users:
            self.users[subject_id]["enabled"] = False

    def find_organization_by_alias(self, alias: str) -> str | None:
        for org_id, org in self.organizations.items():
            if org["alias"] == alias:
                return org_id
        return None

    def create_organization(self, *, name: str, alias: str) -> str:
        # Real Keycloak returns 409 on a duplicate organization alias. The fake
        # MUST refuse too — the same argument create_user makes: a fake more
        # permissive than production is how a duplicate-tenant bug would sail
        # through a green suite and only surface against a real Keycloak.
        if self.find_organization_by_alias(alias) is not None:
            raise KeycloakAdminError(
                f"create organization failed: 409 duplicate alias {alias!r}"
            )
        self._org_counter += 1
        org_id = f"kc-org-{alias}-{self._org_counter}"
        self.organizations[org_id] = {"name": name, "alias": alias, "members": set()}
        return org_id

    def add_member(self, org_id: str, subject_id: str) -> None:
        org = self.organizations.get(org_id)
        if org is None:
            raise KeycloakAdminError(f"add member failed: unknown organization {org_id!r}")
        members = org["members"]
        assert isinstance(members, set)
        members.add(subject_id)  # set semantics = idempotent, mirrors the 409 swallow
