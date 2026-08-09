"""Derive the CLOUD Keycloak realm from the dev realm (Pillar K3).

The dev realm (keycloak/realm-usali.json) is the source of truth for
clients, roles, personas, and mappers — but it carries dev credentials
(`devpass` logins, a hardcoded `usali-admin` client secret) and
localhost redirect URIs. The cloud realm is the SAME realm with:

  - every user `credentials` block STRIPPED (persona passwords are set
    post-deploy via the admin API, from Secret Manager — never baked
    into an image a registry could leak),
  - every confidential client secret replaced with the sentinel
    `ROTATED-AT-DEPLOY` (set post-deploy the same way),
  - the public clients' redirectUris/webOrigins swapped to the ONE
    public app hostname.

Usage:
    uv run python scripts/cloud/make_cloud_realm.py \
        --app-host https://demo.example.com --out <path>
"""

import argparse
import copy
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEV_REALM = REPO_ROOT / "keycloak" / "realm-usali.json"

SECRET_SENTINEL = "ROTATED-AT-DEPLOY"


LOGIN_THEME = "open-hospitality"
DISPLAY_NAME = "Open Hospitality"


def transform(realm: dict, *, app_host: str) -> dict:
    cloud = copy.deepcopy(realm)
    for user in cloud.get("users", []):
        user.pop("credentials", None)
    for client in cloud.get("clients", []):
        if client.get("publicClient", False):
            client["redirectUris"] = [f"{app_host}/*"]
            client["webOrigins"] = [app_host]
        elif "secret" in client:
            client["secret"] = SECRET_SENTINEL
    # The Open Hospitality login skin (branding). Set here for a
    # first-boot import; configure_auth re-applies it to an already
    # imported realm, since --import-realm is first-boot-only.
    cloud["loginTheme"] = LOGIN_THEME
    cloud["displayName"] = DISPLAY_NAME
    cloud["displayNameHtml"] = DISPLAY_NAME
    return cloud


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-host", required=True,
                        help="https://demo.example.com — no trailing slash")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    cloud = transform(
        json.loads(DEV_REALM.read_text()),
        app_host=args.app_host.rstrip("/"),
    )
    args.out.write_text(json.dumps(cloud, indent=2) + "\n")
    print(f"cloud realm written: {args.out} "
          "(credentials stripped, client secrets ROTATED-AT-DEPLOY)")


if __name__ == "__main__":
    main()
