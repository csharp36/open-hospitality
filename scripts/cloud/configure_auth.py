"""Post-deploy Keycloak credential pass (K3).

The cloud realm imports with NO credentials (make_cloud_realm.py). This
script sets, from Secret Manager, via the admin REST API:

  - the `usali-admin` confidential client's secret
    (secret: usali-admin-client-secret — the app's Keycloak admin
    client, K4),
  - each demo persona's password
    (secret: usali-demo-persona-passwords, a username->password JSON).

Idempotent: setting the same secret/passwords again converges. No
credential is ever printed; API errors surface status codes only.

Usage:
    uv run python scripts/cloud/configure_auth.py \
        --project <id> --auth-url https://usali-auth-....run.app
"""

import argparse
import json
import subprocess
import urllib.parse

import httpx

# Realm-management roles the usali-admin SERVICE ACCOUNT needs at RUNTIME
# (issue #69). This script does its own org/user setup with the MASTER admin
# token, so the service account was never granted anything -- and self-service
# signup (provision_tenant) is the FIRST thing to drive the usali-admin client
# against the admin API. Without these it 403s on `GET /organizations` and no
# tenant is ever created.
#
# `manage-realm` is what authorises the ORGANIZATIONS endpoints. That is not
# obvious, and it was measured on throwaway containers against BOTH the
# deployed 26.0.8 and 26.3.5:
#
#     manage-realm alone .................... GET 200 / POST 201
#     manage-realm + view-realm ............. GET 200 / POST 201
#     manage-users view-users query-users ... GET 403 / POST 403
#     view-realm alone ...................... GET 403 / POST 403
#     no roles .............................. GET 403 / POST 403
#
# Do NOT reach for `manage-organizations` / `view-organizations`. Those roles
# DO NOT EXIST on realm-management in 26.0.8, and creating a realm with
# `organizationsEnabled=true` seeds zero org roles on 26.3.5 either -- so a
# version bump does not produce them. Keycloak authorises the org endpoints on
# its own seeded permissions, never on a same-named role, which is why
# hand-creating one still 403s.
#
# `manage-realm` is broader than we would like (it carries realm-configuration
# rights), but Keycloak offers nothing finer for organizations here. It is
# still far narrower than `realm-admin`, which also works and grants
# everything. The user roles are the rest of what provision_tenant does:
# create the workspace's first admin user and assign its roles.
_RUNTIME_RM_ROLES = ("manage-realm", "manage-users", "view-users", "query-users")
# Fail loudly if the realm cannot satisfy the two that carry the flow, rather
# than granting a partial set and 403-ing later at signup.
_RUNTIME_RM_REQUIRED = {"manage-realm", "manage-users"}


def _secret(project: str, name: str) -> str:
    return subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest",
         f"--secret={name}", f"--project={project}"],
        check=True, capture_output=True, text=True,
    ).stdout


def _request(method: str, url: str, *, token: str | None = None,
             form: dict[str, str] | None = None, body: object = None) -> bytes:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = httpx.request(method, url, headers=headers, data=form,
                         json=body, timeout=30)
    if resp.status_code >= 400:
        # Status only — never echo a response body that could carry
        # credential-adjacent payloads into terminal scrollback.
        raise SystemExit(
            f"ERROR: {method} {url.split('?')[0]} "
            f"-> HTTP {resp.status_code}"
        )
    return resp.content


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--auth-url", required=True,
                        help="base URL of the deployed auth service")
    args = parser.parse_args()
    base = args.auth_url.rstrip("/")

    def admin_token() -> str:
        # The master-realm admin access token is short-lived; re-mint it
        # before each phase so a slow Keycloak cold start can't expire it
        # mid-pass (observed: HTTP 401 on the trailing realm PUT).
        data: dict[str, object] = json.loads(_request(
            "POST", f"{base}/realms/master/protocol/openid-connect/token",
            form={
                "grant_type": "password", "client_id": "admin-cli",
                "username": "admin",
                "password": _secret(args.project, "keycloak-admin-password"),
            },
        ))
        return str(data["access_token"])

    token = admin_token()

    clients = json.loads(_request(
        "GET", f"{base}/admin/realms/usali/clients?clientId=usali-admin",
        token=token,
    ))
    if len(clients) != 1:
        raise SystemExit("ERROR: expected exactly one usali-admin client")
    client = clients[0]
    client["secret"] = _secret(args.project, "usali-admin-client-secret")
    _request("PUT", f"{base}/admin/realms/usali/clients/{client['id']}",
             token=token, body=client)
    print("usali-admin client secret: set")

    # Grant that client's service account the runtime roles (see
    # _RUNTIME_RM_ROLES above for what each is for and why manage-realm).
    sa_user = json.loads(_request(
        "GET",
        f"{base}/admin/realms/usali/clients/{client['id']}/service-account-user",
        token=token,
    ))
    rm_clients = json.loads(_request(
        "GET", f"{base}/admin/realms/usali/clients?clientId=realm-management",
        token=token,
    ))
    if len(rm_clients) != 1:
        raise SystemExit("ERROR: expected exactly one realm-management client")
    rm_id = str(rm_clients[0]["id"])
    rm_roles = {
        str(r["name"]): r
        for r in json.loads(_request(
            "GET", f"{base}/admin/realms/usali/clients/{rm_id}/roles",
            token=token,
        ))
    }
    if not _RUNTIME_RM_REQUIRED.issubset(rm_roles):
        raise SystemExit(
            "ERROR: realm-management is missing roles that signup needs: "
            f"{sorted(_RUNTIME_RM_REQUIRED - rm_roles.keys())}"
        )
    # Idempotent: re-POSTing already-assigned client roles is a 204 no-op.
    _request(
        "POST",
        f"{base}/admin/realms/usali/users/{sa_user['id']}"
        f"/role-mappings/clients/{rm_id}",
        token=token,
        body=[rm_roles[name] for name in _RUNTIME_RM_ROLES if name in rm_roles],
    )
    granted = ", ".join(n for n in _RUNTIME_RM_ROLES if n in rm_roles)
    print(f"usali-admin service account: realm-management roles granted ({granted})")

    personas = json.loads(
        _secret(args.project, "usali-demo-persona-passwords")
    )
    for username, password in personas.items():
        found = json.loads(_request(
            "GET",
            f"{base}/admin/realms/usali/users"
            f"?username={urllib.parse.quote(username)}&exact=true",
            token=token,
        ))
        if len(found) != 1:
            raise SystemExit(f"ERROR: persona {username} not found in realm")
        _request(
            "PUT",
            f"{base}/admin/realms/usali/users/{found[0]['id']}/reset-password",
            token=token,
            body={"type": "password", "value": password, "temporary": False},
        )
        print(f"{username}: password set")

    # Branding (Open Hospitality). --import-realm is first-boot-only, so
    # a realm already in the DB won't pick up loginTheme/displayName from
    # the realm JSON — apply it to the live realm here (idempotent). A
    # fresh token: this is the heaviest call (full realm representation),
    # so the original grant may have lapsed. organizationsEnabled rides
    # the same PUT: it must be on BEFORE the organization step below.
    token = admin_token()
    realm = json.loads(_request("GET", f"{base}/admin/realms/usali",
                                token=token))
    realm["loginTheme"] = "open-hospitality"
    realm["displayName"] = "Open Hospitality"
    realm["displayNameHtml"] = "Open Hospitality"
    realm["organizationsEnabled"] = True
    _request("PUT", f"{base}/admin/realms/usali", token=token, body=realm)
    print("realm branding (login theme + display name): set")

    # The founding organization (L3, decision 3 — plan open question 3,
    # resolved): KC 26 realm import DOES carry the organizations config —
    # verified against keycloak:26.0 (26.0.8): `organizationsEnabled`,
    # `organizations`, and members-by-username reference all land on a
    # first-boot --import-realm — so a FRESH deployment needs none of
    # this block. But import is first-boot-only (the K3 lesson): the
    # LIVE realm predates the orgs config and will never re-import it,
    # so the same end state is ensured here, idempotently (the persona-
    # password posture — find, create only when missing, converge on
    # re-run):
    #   - the founding organization, find-or-created by ALIAS (the join
    #     key organization.kc_org_alias resolves against — L3 pins the
    #     literal in tests/test_oidc_realm_contract.py),
    #   - each persona's membership,
    #   - the built-in `organization` client scope as a DEFAULT scope of
    #     operator-portal: Keycloak only emits the membership claim when
    #     the literal `organization` scope is granted, and the SPA does
    #     not (and must not need to) request it.
    token = admin_token()
    org_alias = "pilot-hotel-group"

    def find_org() -> dict[str, object] | None:
        # List-and-filter on the ALIAS: the search parameter matches
        # name/domain, NOT alias (verified against keycloak:26.0 —
        # ?search=<alias>&exact=true returns [] for an org whose alias
        # is exactly that), so a search-keyed find-or-create would
        # re-POST forever and die on the 409.
        # PAGINATION ASSUMPTION: one page of 200 covers every org this
        # deployment can have until L6 self-service exists. If growth
        # ever exceeds it, the miss is LOUD, not silent: find_org
        # returns None, the create POST hits the alias conflict, and
        # _request raises SystemExit on the 409 — paginate then.
        orgs: list[dict[str, object]] = json.loads(_request(
            "GET",
            f"{base}/admin/realms/usali/organizations?first=0&max=200",
            token=token,
        ))
        return next((o for o in orgs if o.get("alias") == org_alias), None)

    org = find_org()
    if org is None:
        _request(
            "POST", f"{base}/admin/realms/usali/organizations",
            token=token,
            body={
                "name": "Pilot Hotel Group",
                "alias": org_alias,
                "enabled": True,
                # KC 26 requires at least one domain; this one is a
                # placeholder identity, not mail routing.
                "domains": [{"name": f"{org_alias}.example",
                             "verified": False}],
            },
        )
        org = find_org()
    if org is None:
        raise SystemExit("ERROR: organization missing after create")
    print(f"organization {org_alias}: present")

    # Unpaginated members GET: fine for the four personas this script
    # owns. If a page limit ever hides an existing member, the re-add
    # POST below dies LOUDLY on the 409 (SystemExit) — paginate then.
    members = {
        m.get("username")
        for m in json.loads(_request(
            "GET",
            f"{base}/admin/realms/usali/organizations/{org['id']}/members",
            token=token,
        ))
    }
    for username in personas:
        if username in members:
            continue
        found = json.loads(_request(
            "GET",
            f"{base}/admin/realms/usali/users"
            f"?username={urllib.parse.quote(username)}&exact=true",
            token=token,
        ))
        if len(found) != 1:
            raise SystemExit(f"ERROR: persona {username} not found in realm")
        # The members endpoint takes the raw user id as a JSON string
        # (Keycloak >= 26.0.6 contract).
        _request(
            "POST",
            f"{base}/admin/realms/usali/organizations/{org['id']}/members",
            token=token, body=found[0]["id"],
        )
        print(f"{username}: organization membership added")

    clients = json.loads(_request(
        "GET",
        f"{base}/admin/realms/usali/clients?clientId=operator-portal",
        token=token,
    ))
    if len(clients) != 1:
        raise SystemExit("ERROR: expected exactly one operator-portal client")
    portal_id = clients[0]["id"]
    scopes = json.loads(_request(
        "GET", f"{base}/admin/realms/usali/client-scopes", token=token,
    ))
    org_scope = next(
        (s for s in scopes if s.get("name") == "organization"), None
    )
    if org_scope is None:
        raise SystemExit("ERROR: built-in `organization` client scope "
                         "missing — is organizationsEnabled set?")
    defaults = {
        s.get("name")
        for s in json.loads(_request(
            "GET",
            f"{base}/admin/realms/usali/clients/{portal_id}"
            "/default-client-scopes",
            token=token,
        ))
    }
    if "organization" not in defaults:
        # Drop a stale OPTIONAL assignment first (Keycloak auto-assigns
        # the scope as optional when organizations are enabled).
        optionals = json.loads(_request(
            "GET",
            f"{base}/admin/realms/usali/clients/{portal_id}"
            "/optional-client-scopes",
            token=token,
        ))
        if any(s.get("name") == "organization" for s in optionals):
            _request(
                "DELETE",
                f"{base}/admin/realms/usali/clients/{portal_id}"
                f"/optional-client-scopes/{org_scope['id']}",
                token=token,
            )
        _request(
            "PUT",
            f"{base}/admin/realms/usali/clients/{portal_id}"
            f"/default-client-scopes/{org_scope['id']}",
            token=token,
        )
    print("operator-portal: `organization` scope is a default scope")

    print("configure_auth complete (no credential printed)")


if __name__ == "__main__":
    main()
