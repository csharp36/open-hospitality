"""Mock QuickBooks Online server (P8 Task 3).

Pure in-memory FastAPI app — no database fixtures. Covers the OAuth token
lifecycle (grant, refresh, expiry), journal-entry validation (balance, CoA
membership, required fields), requestid idempotent replay, the Account query,
and the X-Mock-Fault one-shot fault injection.
"""

from decimal import Decimal

from fastapi.testclient import TestClient

from usali.qbo_mock import create_mock_qbo

BASIC_AUTH = {"Authorization": "Basic Y2xpZW50OnNlY3JldA=="}  # client:secret
REALM = "9341453907771234"


def _client(**kwargs: float) -> TestClient:
    return TestClient(create_mock_qbo(**kwargs))


def _grant(client: TestClient) -> dict[str, object]:
    resp = client.post(
        "/oauth2/v1/tokens/bearer",
        data={"grant_type": "authorization_code", "code": "test-bootstrap"},
        headers=BASIC_AUTH,
    )
    assert resp.status_code == 200, resp.text
    body: dict[str, object] = resp.json()
    return body


def _bearer(client: TestClient) -> dict[str, str]:
    tokens = _grant(client)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _line(posting_type: str, account: str, amount: object) -> dict[str, object]:
    return {
        "Amount": amount,
        "DetailType": "JournalEntryLineDetail",
        "JournalEntryLineDetail": {
            "PostingType": posting_type,
            "AccountRef": {"value": account},
        },
    }


def _je(*lines: dict[str, object]) -> dict[str, object]:
    return {"TxnDate": "2026-07-07", "PrivateNote": "USALI push", "Line": list(lines)}


BALANCED = _je(_line("Debit", "1000", "100.00"), _line("Credit", "4000", "100.00"))


def _post_je(
    client: TestClient,
    body: dict[str, object],
    headers: dict[str, str],
    requestid: str | None = None,
) -> object:
    params = {"requestid": requestid} if requestid else None
    return client.post(f"/v3/company/{REALM}/journalentry", json=body, params=params,
                       headers=headers)


# --- OAuth token lifecycle --------------------------------------------------------


def test_token_grant_shape():
    body = _grant(_client())
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert isinstance(body["access_token"], str) and body["access_token"]
    assert isinstance(body["refresh_token"], str) and body["refresh_token"]
    expiry = body["x_refresh_token_expires_in"]
    assert isinstance(expiry, int) and expiry > 0


def test_default_refresh_token_pre_seeded_and_rotates():
    """The settings-default token "mock" works out of the box, then rotates.

    This is what makes the documented `usali qbo-mock` + `usali qbo-push`
    flow work with no config: the client's first refresh grant uses the
    USALI_QBO_REFRESH_TOKEN default against a fresh mock.
    """
    client = _client()
    resp = client.post(
        "/oauth2/v1/tokens/bearer",
        data={"grant_type": "refresh_token", "refresh_token": "mock"},
        headers=BASIC_AUTH,
    )
    assert resp.status_code == 200, resp.text
    tokens = resp.json()
    assert tokens["refresh_token"] != "mock"
    # Consumed like any other token: the second use of "mock" is rejected.
    stale = client.post(
        "/oauth2/v1/tokens/bearer",
        data={"grant_type": "refresh_token", "refresh_token": "mock"},
        headers=BASIC_AUTH,
    )
    assert stale.status_code == 400
    assert stale.json()["error"] == "invalid_grant"
    # The minted access token is accepted by the API.
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    ok = _post_je(client, BALANCED, headers)
    assert ok.status_code == 200, ok.text


def test_token_refresh_rotates_and_new_access_token_works():
    client = _client()
    first = _grant(client)
    resp = client.post(
        "/oauth2/v1/tokens/bearer",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        headers=BASIC_AUTH,
    )
    assert resp.status_code == 200, resp.text
    second = resp.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    # The rotated-out refresh token no longer works.
    stale = client.post(
        "/oauth2/v1/tokens/bearer",
        data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        headers=BASIC_AUTH,
    )
    assert stale.status_code == 400
    assert stale.json()["error"] == "invalid_grant"
    # The new access token is accepted by the API.
    headers = {"Authorization": f"Bearer {second['access_token']}"}
    ok = _post_je(client, BALANCED, headers)
    assert ok.status_code == 200, ok.text


def test_token_endpoint_requires_basic_auth_header():
    client = _client()
    resp = client.post(
        "/oauth2/v1/tokens/bearer",
        data={"grant_type": "authorization_code", "code": "x"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_client"


def test_token_endpoint_rejects_unknown_grant_type():
    client = _client()
    resp = client.post(
        "/oauth2/v1/tokens/bearer",
        data={"grant_type": "password", "code": "x"},
        headers=BASIC_AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_expired_access_token_rejected():
    client = _client(token_ttl_seconds=0.0)  # tokens are born expired
    headers = _bearer(client)
    resp = _post_je(client, BALANCED, headers)
    assert resp.status_code == 401
    assert resp.json()["fault"]["type"] == "AUTHENTICATION"


def test_missing_and_unknown_bearer_rejected():
    client = _client()
    no_auth = _post_je(client, BALANCED, {})
    assert no_auth.status_code == 401
    assert no_auth.json()["fault"]["type"] == "AUTHENTICATION"
    bogus = _post_je(client, BALANCED, {"Authorization": "Bearer not-a-real-token"})
    assert bogus.status_code == 401
    assert bogus.json()["fault"]["type"] == "AUTHENTICATION"


# --- Journal entry post -----------------------------------------------------------


def test_balanced_je_accepted_and_echoed():
    client = _client()
    headers = _bearer(client)
    resp = _post_je(client, BALANCED, headers)
    assert resp.status_code == 200, resp.text
    je = resp.json()["JournalEntry"]
    assert je["Id"] == "1"
    assert je["TxnDate"] == "2026-07-07"
    assert len(je["Line"]) == 2
    stored = client.get("/mock/journalentries").json()
    assert len(stored) == 1 and stored[0]["Id"] == "1"


def test_amounts_accepted_as_string_or_number_balance_checked_as_decimal():
    client = _client()
    headers = _bearer(client)
    # 0.1 + 0.2 != 0.3 in binary floats; Decimal(str(...)) balance must accept this.
    body = _je(
        _line("Debit", "1000", 0.1),
        _line("Debit", "1100", 0.2),
        _line("Credit", "4000", "0.30"),
    )
    resp = _post_je(client, body, headers)
    assert resp.status_code == 200, resp.text


def test_negative_and_zero_amounts_rejected():
    client = _client()
    headers = _bearer(client)
    # Direction is PostingType's job — a sign-flipped builder must be caught.
    negative = _je(_line("Debit", "1000", "-100.00"), _line("Credit", "4000", "-100.00"))
    resp = _post_je(client, negative, headers)
    assert resp.status_code == 400
    assert resp.json()["Fault"]["type"] == "ValidationFault"
    zero = _je(_line("Debit", "1000", 0), _line("Credit", "4000", 0))
    resp = _post_je(client, zero, headers)
    assert resp.status_code == 400
    assert "positive" in resp.json()["Fault"]["Error"][0]["Detail"]
    assert client.get("/mock/journalentries").json() == []


def test_nan_and_infinite_amounts_rejected():
    client = _client()
    headers = _bearer(client)
    # Decimal("NaN") passes construction but any comparison raises
    # InvalidOperation — an unguarded mock would 500 instead of 400.
    for bad in ("NaN", "Infinity", "-Infinity"):
        body = _je(_line("Debit", "1000", bad), _line("Credit", "4000", bad))
        resp = _post_je(client, body, headers)
        assert resp.status_code == 400, (bad, resp.text)
        assert resp.json()["Fault"]["type"] == "ValidationFault"
    assert client.get("/mock/journalentries").json() == []


def test_line_missing_detail_type_rejected():
    client = _client()
    headers = _bearer(client)
    no_detail_type = {k: v for k, v in _line("Debit", "1000", "10.00").items()
                      if k != "DetailType"}
    resp = _post_je(client, _je(no_detail_type, _line("Credit", "4000", "10.00")), headers)
    assert resp.status_code == 400
    assert "DetailType" in resp.json()["Fault"]["Error"][0]["Message"]


def test_requestid_over_fifty_chars_rejected():
    client = _client()
    headers = _bearer(client)
    ok = _post_je(client, BALANCED, headers, requestid="x" * 50)
    assert ok.status_code == 200, ok.text
    too_long = _post_je(client, BALANCED, headers, requestid="x" * 51)
    assert too_long.status_code == 400
    assert "requestid" in too_long.json()["Fault"]["Error"][0]["Message"]
    assert len(client.get("/mock/journalentries").json()) == 1


def test_accept_header_must_admit_json():
    client = _client()
    headers = _bearer(client)
    # Real QBO v3 would answer these in XML; the mock refuses loudly instead.
    xml_only = {**headers, "Accept": "application/xml"}
    resp = _post_je(client, BALANCED, xml_only)
    assert resp.status_code == 400
    assert "Accept" in resp.json()["error"]
    query = client.get(
        f"/v3/company/{REALM}/query",
        params={"query": "select * from Account"},
        headers=xml_only,
    )
    assert query.status_code == 400
    # Explicit application/json (not just httpx's */* default) is accepted.
    ok = _post_je(client, BALANCED, {**headers, "Accept": "application/json"})
    assert ok.status_code == 200, ok.text


def test_unbalanced_je_rejected():
    client = _client()
    headers = _bearer(client)
    body = _je(_line("Debit", "1000", "100.00"), _line("Credit", "4000", "99.99"))
    resp = _post_je(client, body, headers)
    assert resp.status_code == 400
    fault = resp.json()["Fault"]
    assert fault["type"] == "ValidationFault"
    assert "balance" in fault["Error"][0]["Message"].lower()
    assert client.get("/mock/journalentries").json() == []


def test_unknown_account_rejected():
    client = _client()
    headers = _bearer(client)
    body = _je(_line("Debit", "9999", "50.00"), _line("Credit", "4000", "50.00"))
    resp = _post_je(client, body, headers)
    assert resp.status_code == 400
    assert resp.json()["Fault"]["type"] == "ValidationFault"


def test_line_missing_required_fields_rejected():
    client = _client()
    headers = _bearer(client)
    no_posting: dict[str, object] = {
        "Amount": "10.00",
        "DetailType": "JournalEntryLineDetail",
        "JournalEntryLineDetail": {"AccountRef": {"value": "1000"}},
    }
    resp = _post_je(client, _je(no_posting, _line("Credit", "4000", "10.00")), headers)
    assert resp.status_code == 400
    no_amount = {k: v for k, v in _line("Debit", "1000", "10.00").items() if k != "Amount"}
    resp = _post_je(client, _je(no_amount, _line("Credit", "4000", "10.00")), headers)
    assert resp.status_code == 400
    resp = _post_je(client, {"TxnDate": "2026-07-07", "Line": []}, headers)
    assert resp.status_code == 400


def test_requestid_replay_returns_identical_body_without_double_post():
    client = _client()
    headers = _bearer(client)
    first = _post_je(client, BALANCED, headers, requestid="req-42")
    assert first.status_code == 200, first.text
    replay = _post_je(client, BALANCED, headers, requestid="req-42")
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(client.get("/mock/journalentries").json()) == 1
    # A different requestid is a genuinely new post.
    other = _post_je(client, BALANCED, headers, requestid="req-43")
    assert other.status_code == 200
    assert other.json()["JournalEntry"]["Id"] != first.json()["JournalEntry"]["Id"]
    assert len(client.get("/mock/journalentries").json()) == 2


# --- Account query ----------------------------------------------------------------


def test_account_query_returns_chart_of_accounts():
    client = _client()
    headers = _bearer(client)
    resp = client.get(
        f"/v3/company/{REALM}/query",
        params={"query": "select * from Account"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    accounts = resp.json()["QueryResponse"]["Account"]
    assert {a["AcctNum"] for a in accounts} == {
        "1000", "1100", "1200", "1210", "2100", "4000", "4100", "4200", "6000",
    }
    room = next(a for a in accounts if a["AcctNum"] == "4000")
    assert room["Name"] == "Room Revenue"
    assert room["AccountType"] == "Income"
    assert room["Id"]


def test_account_query_requires_bearer():
    client = _client()
    resp = client.get(f"/v3/company/{REALM}/query", params={"query": "select * from Account"})
    assert resp.status_code == 401


def test_unsupported_query_rejected():
    client = _client()
    headers = _bearer(client)
    resp = client.get(
        f"/v3/company/{REALM}/query",
        params={"query": "select * from Invoice"},
        headers=headers,
    )
    assert resp.status_code == 400


# --- Fault injection --------------------------------------------------------------


def test_throttle_once_then_success():
    client = _client()
    headers = _bearer(client)
    fault = {**headers, "X-Mock-Fault": "throttle-once:abc123"}
    resp = _post_je(client, BALANCED, fault)
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1"
    assert client.get("/mock/journalentries").json() == []
    retry = _post_je(client, BALANCED, fault)
    assert retry.status_code == 200, retry.text
    # A distinct nonce is its own one-shot fault.
    other = _post_je(client, BALANCED, {**headers, "X-Mock-Fault": "throttle-once:def456"})
    assert other.status_code == 429


def test_expired_once_then_success():
    client = _client()
    headers = _bearer(client)
    fault = {**headers, "X-Mock-Fault": "expired-once:xyz"}
    resp = _post_je(client, BALANCED, fault)
    assert resp.status_code == 401
    assert resp.json()["fault"]["type"] == "AUTHENTICATION"
    retry = _post_je(client, BALANCED, fault)
    assert retry.status_code == 200, retry.text


def test_state_is_per_app_instance():
    a, b = _client(), _client()
    _post_je(a, BALANCED, _bearer(a))
    assert len(a.get("/mock/journalentries").json()) == 1
    assert b.get("/mock/journalentries").json() == []


def test_decimal_amounts_survive_roundtrip():
    client = _client()
    headers = _bearer(client)
    body = _je(_line("Debit", "1000", "123.45"), _line("Credit", "4000", "123.45"))
    resp = _post_je(client, body, headers)
    assert resp.status_code == 200, resp.text
    echoed = resp.json()["JournalEntry"]["Line"][0]["Amount"]
    assert Decimal(str(echoed)) == Decimal("123.45")
