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
from typing import NoReturn

import httpx

_REDIRECTS = (301, 302, 303, 307, 308)
_PASSWORD_FIELD = re.compile(r'type="password"', re.I)


def _secret(project: str, name: str) -> str:
    return subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest",
         f"--secret={name}", f"--project={project}"],
        check=True, capture_output=True, text=True,
    ).stdout


def _fail(step: str, resp: httpx.Response) -> NoReturn:
    # Status only — response bodies stay off the terminal.
    raise SystemExit(f"ERROR: {step} -> HTTP {resp.status_code}")


def _form_action(page_html: str) -> str:
    """The (unescaped) action URL of the first form on a login page."""
    m = re.search(r'action="([^"]+)"', page_html)
    if m is None:
        raise SystemExit("ERROR: no login form on the auth page")
    return html.unescape(m.group(1))


def _redirect_code(resp: httpx.Response) -> str | None:
    """The `code` param from a 3xx Location, or None (not a redirect / no code)."""
    if resp.status_code not in _REDIRECTS:
        return None
    location = resp.headers.get("location", "")
    return urllib.parse.parse_qs(
        urllib.parse.urlsplit(location).query
    ).get("code", [None])[0]


def _obtain_code(
    client: httpx.Client, login_page: httpx.Response, username: str,
    password: str, public: str, base: str,
) -> str:
    """Drive the login form to an authorization code.

    Handles BOTH login shapes: the classic COMBINED username+password form,
    and the USERNAME-FIRST flow Keycloak switches to once the realm has the
    Organizations feature enabled (a username/email page, THEN a password
    page — so KC can route to a per-org identity provider by username). The
    old single-POST assumption silently broke when organizations were turned
    on for the demo: the username page ignores the password field and just
    renders the password page (HTTP 200), never the redirect the caller
    expected.
    """
    def submit(page_html: str, fields: dict[str, str]) -> httpx.Response:
        return client.post(_rebase(_form_action(page_html), public, base), data=fields)

    if _PASSWORD_FIELD.search(login_page.text):  # combined form
        resp = submit(login_page.text, {"username": username, "password": password})
    else:  # username-first: username page, then password page
        resp = submit(login_page.text, {"username": username})
        # A redirect without a code is the hop TO the password page — follow it.
        if resp.status_code in _REDIRECTS and _redirect_code(resp) is None:
            resp = client.get(_rebase(html.unescape(resp.headers["location"]), public, base))
        if resp.status_code == 200 and _PASSWORD_FIELD.search(resp.text):
            resp = submit(resp.text, {"password": password})

    code = _redirect_code(resp)
    if code is None:
        _fail("login (wrong persona password, or unexpected login flow)", resp)
    return code


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

        code = _obtain_code(
            client, login_page, args.username, password, public, base
        )

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
