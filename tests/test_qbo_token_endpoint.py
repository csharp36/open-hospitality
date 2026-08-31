"""The QBO token endpoint's failure modes (OH-17, hardened 2026-08-31).

Every test here exists because a review found the failure mode reachable
from `integrations_api.callback`, which is the ONE route in the app with no
authentication at all. Two properties are load-bearing:

  * nothing from an upstream body reaches a QboError, because the callback
    interpolates the QboError into its 400 — a proxy error page carries
    internal hostnames and can reflect the authorization code;
  * an unreachable or misbehaving token endpoint is an ordinary refusal, not
    an unhandled exception, because on that route an unhandled exception is a
    500 that anyone on the internet can trigger.
"""

from typing import Any

import httpx
import pytest

from usali.qbo_client import (
    QboClient,
    QboError,
    QboUnreachable,
    StaticTokenStore,
    exchange_authorization_code,
)

_SECRET_BODY = (
    "<html>upstream 10.0.3.7:8443 failed; internal-token=SECRET-abcdefghij; "
    "code=SUPER-SECRET-CODE</html>"
)


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _exchange(handler: Any) -> str:
    return exchange_authorization_code(
        "the-code", base_url="https://token.example", client_id="cid",
        client_secret="sec", redirect_uri="https://app.example/cb",
        transport=_transport(handler),
    )


def _client(handler: Any) -> QboClient:
    return QboClient(
        "https://token.example", "cid", "sec", "realm-1",
        StaticTokenStore("refresh-1"), transport=_transport(handler),
    )


# --------------------------------------------------------------- no echoing


@pytest.mark.parametrize("status", [400, 502])
def test_an_unparseable_error_body_is_never_echoed(status: int) -> None:
    """The leak this file was written for: `_error_message` used to fall back
    to `resp.text[:200]`, so the callback's 400 carried the proxy's HTML."""
    with pytest.raises(QboError) as caught:
        _exchange(lambda request: httpx.Response(status, text=_SECRET_BODY))
    message = str(caught.value)
    assert "SECRET-abcdefghij" not in message
    assert "SUPER-SECRET-CODE" not in message
    assert "10.0.3.7" not in message
    assert "unparseable" in message


def test_a_json_body_in_an_unknown_shape_is_not_echoed_either() -> None:
    """The second fallback. JSON parses, so the `except ValueError` arm above
    does not fire — a different branch reached the same `resp.text[:200]`."""
    with pytest.raises(QboError) as caught:
        _exchange(lambda request: httpx.Response(
            400, json={"weird": {"internal": "10.0.3.7", "secret": "s3cr3t"}}
        ))
    assert "s3cr3t" not in str(caught.value)
    assert "10.0.3.7" not in str(caught.value)


def test_intuits_own_fault_text_IS_surfaced() -> None:
    """The other half of the property, and the reason this is not simply
    "return a constant": an operator must be able to tell "you declined" from
    "that code is already spent", and only Intuit's structured text says
    which. Dropping this assertion would let the leak be "fixed" by making
    every refusal identical and useless."""
    with pytest.raises(QboError, match="already spent"):
        _exchange(lambda request: httpx.Response(400, json={
            "Fault": {"Error": [{"Message": "invalid_grant",
                                 "Detail": "that code is already spent"}]}
        }))


# ------------------------------------------------------ refusals, not 500s


def test_a_200_with_a_non_json_body_is_a_refusal() -> None:
    """`resp.json()` sat after the status check, so a captive portal or an
    incident page served with a 200 raised JSONDecodeError -> 500."""
    with pytest.raises(QboError):
        _exchange(lambda request: httpx.Response(200, text="<html>hello</html>"))


def test_a_network_failure_is_a_refusal_naming_only_the_type() -> None:
    """"Intuit is unreachable" is routine. The exception's own text carries
    the resolved upstream address, so only its TYPE may travel."""
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to 10.0.3.7:443")

    with pytest.raises(QboError) as caught:
        _exchange(boom)
    assert "ConnectError" in str(caught.value)
    assert "10.0.3.7" not in str(caught.value)


def test_a_refresh_that_cannot_reach_intuit_is_a_refusal() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out talking to 10.0.3.7")

    with pytest.raises(QboError) as caught:
        _client(boom).post_journal_entry({"Line": []}, "req-1")
    assert "ReadTimeout" in str(caught.value)
    assert "10.0.3.7" not in str(caught.value)


@pytest.mark.parametrize("payload, missing", [
    ({"refresh_token": "r2"}, "access_token"),
    ({"access_token": "a2"}, "refresh_token"),
])
def test_a_refresh_missing_either_token_is_a_refusal(
    payload: dict[str, str], missing: str
) -> None:
    """Both were bare `payload[...]` KeyErrors. The refresh_token case is the
    worse one: storing None would kill the tenant's connection on the NEXT
    call, long after the request that broke it."""
    with pytest.raises(QboError, match=missing):
        _client(
            lambda request: httpx.Response(200, json=payload)
        ).post_journal_entry({"Line": []}, "req-1")


def test_unreachable_is_a_qbo_error_subclass() -> None:
    """Both halves of the QboUnreachable decision, in one place.

    SUBCLASS, so every existing `except QboError` still covers it — above all
    the OAuth callback's, whose whole job is that no refusal becomes a 500.
    DISTINCT, so `qbo_push.push_day` can re-raise it instead of writing a
    per-date `failed` ledger row: an unreachable endpoint fails identically
    for every date in the run, and recording N rows for one network blip is
    wrong. Collapsing it back into plain QboError would silently restore that
    — the CLI test that would notice is three files away."""
    assert issubclass(QboUnreachable, QboError)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(QboUnreachable):
        _exchange(boom)
