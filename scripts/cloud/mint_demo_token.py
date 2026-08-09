"""Mint an operator token the way a browser would (K4).

The demo personas deliberately have NO direct-access grants (ROPC stays
off — the realm's browser flow is the only login path). So the smoke
authenticates exactly like the SPA: Authorization Code + PKCE against
the deployed Keycloak, driving the login form headlessly with the
persona password from Secret Manager. The resulting token carries the
usali-api audience and the persona's realm role — the real thing, via
the real flow.

Prints the ACCESS TOKEN to stdout (nothing else): callers capture it
into a shell variable for one smoke pass; it expires like any session.

Usage:
    uv run python scripts/cloud/mint_demo_token.py \
        --project <id> --auth-url https://usali-auth-....run.app \
        [--username dev-gm] [--app-host https://demo.example.com]
"""

import argparse
import base64
import hashlib
import html
import json
import re
import secrets
import subprocess
import sys
import urllib.parse

import httpx


def _secret(project: str, name: str) -> str:
    return subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest",
         f"--secret={name}", f"--project={project}"],
        check=True, capture_output=True, text=True,
    ).stdout


def _fail(step: str, resp: httpx.Response) -> None:
    # Status only — response bodies stay off the terminal.
    raise SystemExit(f"ERROR: {step} -> HTTP {resp.status_code}")


def _rebase(url: str, public_base: str, reachable_base: str) -> str:
    """Keycloak generates every URL with KC_HOSTNAME (the public host),
    which may not resolve/serve yet while the managed cert provisions.
    Rebase such URLs onto the reachable run.app base for TRANSPORT —
    same paths, same flow, different socket."""
    if url.startswith(public_base):
        return reachable_base + url[len(public_base):]
    return url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--auth-url", required=True)
    parser.add_argument("--username", default="dev-gm")
    parser.add_argument("--app-host", default="https://demo.example.com")
    parser.add_argument("--public-auth-host",
                        default="https://auth.example.com",
                        help="KC_HOSTNAME value; URLs under it are "
                             "rebased onto --auth-url for transport")
    args = parser.parse_args()
    base = args.auth_url.rstrip("/")
    public = args.public_auth_host.rstrip("/")
    redirect_uri = args.app_host.rstrip("/") + "/"

    password = json.loads(
        _secret(args.project, "usali-demo-persona-passwords")
    )[args.username]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    with httpx.Client(follow_redirects=False, timeout=30) as client:
        login_page = client.get(
            f"{base}/realms/usali/protocol/openid-connect/auth?"
            + urllib.parse.urlencode({
                "client_id": "operator-portal",
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "openid",
                "state": secrets.token_urlsafe(16),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            })
        )
        if login_page.status_code != 200:
            _fail("auth endpoint", login_page)
        match = re.search(r'action="([^"]+)"', login_page.text)
        if match is None:
            raise SystemExit("ERROR: no login form on the auth page")

        submitted = client.post(
            _rebase(html.unescape(match.group(1)), public, base),
            data={"username": args.username, "password": password},
        )
        if submitted.status_code != 302:
            _fail("login form (wrong persona password?)", submitted)
        location = submitted.headers["location"]
        code = urllib.parse.parse_qs(
            urllib.parse.urlsplit(location).query
        ).get("code", [None])[0]
        if code is None:
            raise SystemExit("ERROR: login redirect carried no code")

        token = client.post(
            f"{base}/realms/usali/protocol/openid-connect/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": "operator-portal",
                "code_verifier": verifier,
            },
        )
        if token.status_code != 200:
            _fail("token exchange", token)
        sys.stdout.write(token.json()["access_token"])


if __name__ == "__main__":
    main()
