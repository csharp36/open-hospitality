"""The demo token minter drives Keycloak's login form to an auth code.

Once the realm has the Organizations feature (which the demo enables so the
`organization` membership claim rides operator tokens), Keycloak serves a
USERNAME-FIRST login: a username/email page, then a separate password page.
`_obtain_code` must walk both that flow and the classic combined form — the
single-POST assumption silently regressed the smoke test when organizations
were turned on. These pins drive both shapes (and a bad password) through an
httpx MockTransport, no gcloud or live realm needed.
"""

import importlib.util
from pathlib import Path

import httpx
import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "cloud" / "mint_demo_token.py"
_spec = importlib.util.spec_from_file_location("mint_demo_token", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mint)

_BASE = "https://auth.test"
_USERNAME_PAGE = (
    '<form id="kc-form-login" action="https://auth.test/la?step=username"'
    ' method="post"><input name="username"><button name="login"></button></form>'
)
_PASSWORD_PAGE = (
    '<form id="kc-form-login" action="https://auth.test/la?step=password"'
    ' method="post"><input type="password" name="password"></form>'
)
_COMBINED_PAGE = (
    '<form id="kc-form-login" action="https://auth.test/la" method="post">'
    '<input name="username"><input type="password" name="password"></form>'
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _page(text: str) -> httpx.Response:
    return httpx.Response(200, text=text, request=httpx.Request("GET", f"{_BASE}/auth"))


def test_username_first_flow_walks_both_pages():
    """Username page (no password field) -> password page -> redirect w/ code."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "step=username" in url:
            return _page(_PASSWORD_PAGE)  # KC renders the password page, HTTP 200
        if "step=password" in url:
            return httpx.Response(
                302, headers={"location": f"{_BASE}/cb?code=THE_CODE&state=x"}
            )
        return httpx.Response(500, text="unexpected")

    with _client(handler) as client:
        code = mint._obtain_code(
            client, _page(_USERNAME_PAGE), "dev-gm", "pw", _BASE, _BASE
        )
    assert code == "THE_CODE"


def test_combined_form_flow_posts_both_at_once():
    """Classic single form (password field present) -> redirect w/ code."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://auth.test/la"
        return httpx.Response(302, headers={"location": f"{_BASE}/cb?code=COMBINED"})

    with _client(handler) as client:
        code = mint._obtain_code(
            client, _page(_COMBINED_PAGE), "dev-gm", "pw", _BASE, _BASE
        )
    assert code == "COMBINED"


def test_bad_password_reraises_rather_than_returning_none():
    """A wrong password re-renders the password page (200) — refuse loudly."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "step=username" in str(request.url):
            return _page(_PASSWORD_PAGE)
        return _page(_PASSWORD_PAGE)  # password POST bounces back to the form

    with _client(handler) as client, pytest.raises(SystemExit):
        mint._obtain_code(client, _page(_USERNAME_PAGE), "dev-gm", "nope", _BASE, _BASE)
