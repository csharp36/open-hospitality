"""QBO client + JE builder + idempotent push ledger (P8 Task 4).

Plans are built from the six seeded sample PDFs (business date 2026-07-07)
and pushed against the in-process mock QBO through ``SyncASGITransport`` —
the real client code path end to end, no HTTP socket. HISJ numbers are pinned
from the plan document; SSSJ numbers were derived from the promoted facts and
pinned (revenue 6359.03 + taxes 869.90 = 7228.93; settlements 6790.16;
guest-ledger remainder 438.77; the cash/write-off/other-operated groups net
to zero and are omitted — the mock rejects zero amounts).
"""

import threading
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from usali import qbo_client as qbo_client_module
from usali import qbo_push
from usali.cli import app
from usali.models import QboPushLedger, UsaliFinancialFact
from usali.qbo_client import (
    QboClient,
    QboError,
    StaticTokenStore,
    SyncASGITransport,
    TokenStore,
)
from usali.qbo_mock import create_mock_qbo
from usali.qbo_push import (
    JePlan,
    PushResult,
    UnmappedGlError,
    build_journal_entry,
    fact_dates,
    month_bounds,
    push_day,
    push_month,
    push_ledger_rows,
)
from usali.reporting import NoFactsError

BASIC_AUTH = {"Authorization": "Basic Y2xpZW50OnNlY3JldA=="}  # client:secret
REALM = "mock-realm"
DAY = date(2026, 7, 7)

runner = CliRunner()


@pytest.fixture
def mock_app() -> Any:
    return create_mock_qbo()


@pytest.fixture
def qbo(mock_app: Any) -> QboClient:
    """QboClient wired to the in-process mock via a bootstrapped refresh token."""
    return QboClient(
        "http://mock-qbo",
        "client",
        "secret",
        REALM,
        StaticTokenStore(_bootstrap_refresh_token(mock_app)),
        transport=SyncASGITransport(mock_app),
    )


def _bootstrap_refresh_token(mock_app: Any) -> str:
    """authorization_code grant — the test stand-in for Intuit's consent flow."""
    with httpx.Client(transport=SyncASGITransport(mock_app), base_url="http://mock-qbo") as c:
        resp = c.post(
            "/oauth2/v1/tokens/bearer",
            data={"grant_type": "authorization_code", "code": "bootstrap"},
            headers=BASIC_AUTH,
        )
        assert resp.status_code == 200, resp.text
        token: str = resp.json()["refresh_token"]
        return token


def _stored_jes(mock_app: Any) -> list[dict[str, Any]]:
    with httpx.Client(transport=SyncASGITransport(mock_app), base_url="http://mock-qbo") as c:
        entries: list[dict[str, Any]] = c.get("/mock/journalentries").json()
        return entries


class _RepeatFault(httpx.BaseTransport):
    """Injects a FRESH one-shot fault nonce on every /v3 request, so the mock
    fails every data-plane call with the given fault kind — the way to test
    the client's retry CAPS (a plain one-shot header only fails once)."""

    def __init__(self, mock_app: Any, kind: str) -> None:
        self._inner = SyncASGITransport(mock_app)
        self._kind = kind
        self._count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v3/"):
            self._count += 1
            request.headers["X-Mock-Fault"] = f"{self._kind}:cap-{self._count}"
        return self._inner.handle_request(request)


BALANCED_JE: dict[str, Any] = {
    "TxnDate": "2026-07-07",
    "Line": [
        {
            "Amount": "100.00",
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": "1000"}},
        },
        {
            "Amount": "100.00",
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {"PostingType": "Credit", "AccountRef": {"value": "4000"}},
        },
    ],
}


def _lines(plan: JePlan) -> dict[tuple[str, str], Decimal]:
    out = {(li.gl_account_code, li.posting): li.amount for li in plan.lines}
    assert len(out) == len(plan.lines)  # no duplicate (account, side) lines
    return out


def _bump_fact(session: Session, property_id: str, line_item: str, delta: Decimal) -> None:
    """Shift one fact's amount — the 'facts changed after push' scenario."""
    fact = session.scalars(
        select(UsaliFinancialFact)
        .where(
            UsaliFinancialFact.property_id == property_id,
            UsaliFinancialFact.usali_line_item == line_item,
        )
        .order_by(UsaliFinancialFact.fact_id)
    ).first()
    assert fact is not None
    fact.amount = Decimal(str(fact.amount)) + delta
    session.commit()


def _ledger_row(session: Session, property_id: str) -> QboPushLedger:
    row = session.scalar(
        select(QboPushLedger).where(QboPushLedger.property_id == property_id)
    )
    assert row is not None
    return row


# --- JE builder -------------------------------------------------------------------


def test_hisj_plan_matches_pinned_numbers(db_session, seed_six_pdfs):
    plan = build_journal_entry(db_session, property_id="HISJ", business_date=DAY)
    assert plan.total_debits == plan.total_credits == Decimal("12439.66")
    lines = _lines(plan)
    assert lines[("4000", "Credit")] == Decimal("10395.00")
    assert lines[("4100", "Credit")] == Decimal("61.37")
    assert lines[("4200", "Credit")] == Decimal("410.00")
    assert lines[("2100", "Credit")] == Decimal("1573.29")
    assert lines[("1100", "Debit")] == Decimal("6943.73")
    assert lines[("1200", "Debit")] == Decimal("149.75")
    assert lines[("1210", "Debit")] == Decimal("5346.18")
    assert len(plan.lines) == 7
    # Settlements debit 7093.48 across the clearing accounts.
    assert lines[("1100", "Debit")] + lines[("1200", "Debit")] == Decimal("7093.48")
    assert all(li.amount > 0 for li in plan.lines)
    assert all(li.account_name for li in plan.lines)
    assert len(plan.request_hash) == 64


def test_sssj_plan_derived_and_pinned(db_session, seed_six_pdfs):
    plan = build_journal_entry(db_session, property_id="SSSJ", business_date=DAY)
    assert plan.total_debits == plan.total_credits == Decimal("7228.93")
    lines = _lines(plan)
    assert lines[("4000", "Credit")] == Decimal("6159.03")
    assert lines[("4200", "Credit")] == Decimal("200.00")
    assert lines[("2100", "Credit")] == Decimal("869.90")
    assert lines[("1100", "Debit")] == Decimal("6704.51")
    assert lines[("1200", "Debit")] == Decimal("85.65")
    assert lines[("1210", "Debit")] == Decimal("438.77")
    # Zero-net groups are omitted: other-operated revenue (4100), cash
    # settlements (1000), and the 0.00 write-off (6000).
    posted_accounts = {li.gl_account_code for li in plan.lines}
    assert posted_accounts.isdisjoint({"4100", "1000", "6000"})
    assert len(plan.lines) == 6
    assert lines[("1100", "Debit")] + lines[("1200", "Debit")] == Decimal("6790.16")


def test_non_operating_facts_are_debited_and_reduce_remainder(db_session, seed_six_pdfs):
    # SSSJ's write-off fact is 0.00 in the sample; give it a real value.
    db_session.execute(
        update(UsaliFinancialFact)
        .where(
            UsaliFinancialFact.property_id == "SSSJ",
            UsaliFinancialFact.usali_major_category == "Non-Operating",
        )
        .values(amount=Decimal("-50.00"))
    )
    db_session.commit()
    plan = build_journal_entry(db_session, property_id="SSSJ", business_date=DAY)
    lines = _lines(plan)
    assert lines[("6000", "Debit")] == Decimal("50.00")
    # The write-off debit reduces the guest-ledger remainder, not the totals.
    assert lines[("1210", "Debit")] == Decimal("388.77")
    assert plan.total_debits == plan.total_credits == Decimal("7228.93")


def test_negative_remainder_flips_balancing_line_to_credit(db_session, seed_six_pdfs):
    before = build_journal_entry(db_session, property_id="SSSJ", business_date=DAY)
    fact = db_session.scalars(
        select(UsaliFinancialFact)
        .where(
            UsaliFinancialFact.property_id == "SSSJ",
            UsaliFinancialFact.gl_account_code == "1100",
        )
        .order_by(UsaliFinancialFact.fact_id)
    ).first()
    assert fact is not None
    fact.amount = Decimal(str(fact.amount)) - Decimal("10000.00")
    db_session.commit()
    plan = build_journal_entry(db_session, property_id="SSSJ", business_date=DAY)
    lines = _lines(plan)
    assert lines[("1100", "Debit")] == Decimal("16704.51")
    assert ("1210", "Debit") not in lines
    assert lines[("1210", "Credit")] == Decimal("9561.23")
    assert plan.total_debits == plan.total_credits == Decimal("16790.16")
    assert plan.request_hash != before.request_hash


def test_unmapped_gl_refuses_and_names_the_line(db_session, seed_six_pdfs):
    db_session.execute(
        update(UsaliFinancialFact)
        .where(
            UsaliFinancialFact.property_id == "HISJ",
            UsaliFinancialFact.usali_line_item == "Parking",
        )
        .values(gl_account_code=None)
    )
    db_session.commit()
    with pytest.raises(UnmappedGlError) as excinfo:
        build_journal_entry(db_session, property_id="HISJ", business_date=DAY)
    assert excinfo.value.lines == [("Miscellaneous Income", "Parking", "Parking")]
    assert "Parking" in str(excinfo.value)


def test_no_facts_raises_no_facts_error(db_session, seed_six_pdfs):
    with pytest.raises(NoFactsError):
        build_journal_entry(db_session, property_id="HISJ", business_date=date(2026, 1, 1))


def test_request_hash_stable_then_sensitive_to_amounts(db_session, seed_six_pdfs):
    first = build_journal_entry(db_session, property_id="HISJ", business_date=DAY)
    second = build_journal_entry(db_session, property_id="HISJ", business_date=DAY)
    assert first.request_hash == second.request_hash
    _bump_fact(db_session, "HISJ", "Parking", Decimal("1.00"))
    third = build_journal_entry(db_session, property_id="HISJ", business_date=DAY)
    assert third.request_hash != first.request_hash
    assert third.total_credits == Decimal("12440.66")


# --- push_day lifecycle -----------------------------------------------------------


def test_push_day_lifecycle_pushed_already_pushed_stale(
    db_session, seed_six_pdfs, mock_app, qbo
):
    first = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert first == PushResult(status="pushed", qbo_je_id="1", message=None)
    row = _ledger_row(db_session, "HISJ")
    assert row.status == "pushed" and row.qbo_je_id == "1" and row.message is None
    pushed_hash = row.request_hash
    stored = _stored_jes(mock_app)
    assert len(stored) == 1
    assert stored[0]["TxnDate"] == "2026-07-07"
    # Every posted amount is positive and stringly-exact.
    assert all(Decimal(li["Amount"]) > 0 for li in stored[0]["Line"])

    again = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert again == PushResult(status="already-pushed", qbo_je_id="1", message=None)
    assert len(_stored_jes(mock_app)) == 1

    # Facts change AFTER the push: mark stale, never re-post.
    _bump_fact(db_session, "HISJ", "Parking", Decimal("1.00"))
    stale = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert stale.status == "stale" and stale.qbo_je_id == "1"
    assert stale.message is not None and "changed" in stale.message
    row = _ledger_row(db_session, "HISJ")
    assert row.status == "stale" and row.request_hash == pushed_hash
    assert len(_stored_jes(mock_app)) == 1

    # Still stale on the next attempt — manual correction required.
    still = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert still.status == "stale"
    assert len(_stored_jes(mock_app)) == 1

    # Facts reverted to exactly the pushed content: the JE is correct again.
    _bump_fact(db_session, "HISJ", "Parking", Decimal("-1.00"))
    restored = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert restored == PushResult(status="already-pushed", qbo_je_id="1", message=None)
    assert _ledger_row(db_session, "HISJ").status == "pushed"
    assert len(_stored_jes(mock_app)) == 1


def test_throttle_once_retries_after_backoff(
    db_session, seed_six_pdfs, mock_app, qbo, monkeypatch
):
    sleeps: list[float] = []
    monkeypatch.setattr(qbo_client_module, "_sleep", sleeps.append)
    qbo._http.headers["X-Mock-Fault"] = "throttle-once:test-429"
    result = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert result.status == "pushed"
    assert sleeps == [1.0]  # honored the mock's Retry-After: 1
    assert len(_stored_jes(mock_app)) == 1


def test_expired_token_once_refreshes_and_retries(db_session, seed_six_pdfs, mock_app, qbo):
    # The lazy first refresh consumes (rotates) the bootstrap refresh token, so
    # the 401-triggered refresh below only succeeds if the client persisted the
    # rotated token — this also proves rotation persistence.
    qbo._http.headers["X-Mock-Fault"] = "expired-once:test-401"
    result = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert result.status == "pushed" and result.qbo_je_id == "1"
    assert len(_stored_jes(mock_app)) == 1


class RecordingTokenStore:
    """A TokenStore that keeps its value, so a test can rebuild a client the
    way a NEW PROCESS would and prove the rotation lineage survived."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.stores: list[str] = []

    def load(self) -> str:
        return self.token

    def store(self, refresh_token: str) -> None:
        self.token = refresh_token
        self.stores.append(refresh_token)


def _client_on(mock_app: Any, store: TokenStore) -> QboClient:
    return QboClient(
        "http://mock-qbo", "client", "secret", REALM, store,
        transport=SyncASGITransport(mock_app),
    )


def test_rotation_is_written_through_to_the_store(db_session, seed_six_pdfs, mock_app):
    """D-OH17.7. The lazy first refresh consumes and rotates the bootstrap
    token, so the store must have been written or the NEXT refresh is dead."""
    bootstrap = _bootstrap_refresh_token(mock_app)
    store = RecordingTokenStore(bootstrap)
    result = push_day(db_session, _client_on(mock_app, store),
                      property_id="HISJ", business_date=DAY)
    assert result.status == "pushed"
    assert store.stores, "the rotated refresh token was never persisted"
    # Not just "something was written": the value must have ROTATED. Writing
    # the bootstrap token back would satisfy the assertion above and still
    # leave the next refresh dead against a token Intuit has consumed.
    assert store.stores[-1] != bootstrap


def test_a_rebuilt_client_can_still_refresh(db_session, seed_six_pdfs, mock_app):
    """The regression test for the bug `qbo_client.py` documents against
    itself: rotation used to live in client memory, so a process restart lost
    it and the next push invalid_grant'd against a token Intuit had already
    consumed. Rebuilding from the SAME store is exactly what a restart does.

    The second push MUST be a different property. Re-pushing HISJ would
    short-circuit in `push_day` on the already-pushed ledger row and never
    reach QBO at all, so the test would pass without a single refresh.
    """
    store = RecordingTokenStore(_bootstrap_refresh_token(mock_app))
    first = push_day(db_session, _client_on(mock_app, store),
                     property_id="HISJ", business_date=DAY)
    assert first.status == "pushed"
    rotations_after_first = len(store.stores)

    # A fresh client, as a restarted process would build: same store, new
    # instance, no in-memory state carried over.
    rebuilt = _client_on(mock_app, store)
    rebuilt._http.headers["X-Mock-Fault"] = "expired-once:test-401"
    second = push_day(db_session, rebuilt, property_id="SSSJ", business_date=DAY)
    assert second.status == "pushed", second.message
    # It really refreshed — twice, in fact: the lazy grant plus the one the
    # injected 401 forces. A cached access token could not have carried it.
    assert len(store.stores) == rotations_after_first + 2
    assert len(_stored_jes(mock_app)) == 2


def test_static_store_keeps_the_old_in_memory_behaviour():
    """StaticTokenStore must be a load/store shim over one string and nothing
    more, so the port is a strict ADDITION: the mock, the CLI and the portal's
    settings-built client behave exactly as they did before OH-17. If this
    ever grows persistence, those paths have silently changed semantics."""
    store = StaticTokenStore("tok")
    assert store.load() == "tok"
    store.store("rotated")
    assert store.load() == "rotated"


def test_unbalanced_post_records_failed_row_then_retry_succeeds(
    db_session, seed_six_pdfs, mock_app, qbo, monkeypatch
):
    real_body = qbo_push.journal_entry_body

    def tampered(plan: JePlan) -> dict[str, Any]:
        body = real_body(plan)
        body["Line"][0]["Amount"] = str(Decimal(body["Line"][0]["Amount"]) + 1)
        return body

    monkeypatch.setattr(qbo_push, "journal_entry_body", tampered)
    result = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert result.status == "failed" and result.qbo_je_id is None
    assert result.message is not None and "balance" in result.message.lower()
    row = _ledger_row(db_session, "HISJ")
    assert row.status == "failed" and row.qbo_je_id is None and row.message == result.message
    assert _stored_jes(mock_app) == []

    # Failed rows are retryable: with the builder fixed, the same day pushes.
    monkeypatch.setattr(qbo_push, "journal_entry_body", real_body)
    retried = push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    assert retried.status == "pushed"
    assert _ledger_row(db_session, "HISJ").status == "pushed"
    assert len(_stored_jes(mock_app)) == 1


def test_third_consecutive_429_exhausts_retries(mock_app, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(qbo_client_module, "_sleep", sleeps.append)
    client = QboClient(
        "http://mock-qbo",
        "client",
        "secret",
        REALM,
        StaticTokenStore(_bootstrap_refresh_token(mock_app)),
        transport=_RepeatFault(mock_app, "throttle-once"),
    )
    with pytest.raises(QboError) as excinfo:
        client.post_journal_entry(BALANCED_JE, request_id="throttle-cap")
    assert excinfo.value.status == 429
    assert sleeps == [1.0, 1.0]  # three POST attempts, two backoffs, then give up
    assert _stored_jes(mock_app) == []


def test_second_consecutive_401_raises_after_single_refresh(mock_app):
    client = QboClient(
        "http://mock-qbo",
        "client",
        "secret",
        REALM,
        StaticTokenStore(_bootstrap_refresh_token(mock_app)),
        transport=_RepeatFault(mock_app, "expired-once"),
    )
    # 401 -> refresh once -> retry -> 401 again -> QboError, never a loop.
    with pytest.raises(QboError) as excinfo:
        client.post_journal_entry(BALANCED_JE, request_id="expired-cap")
    assert excinfo.value.status == 401
    assert _stored_jes(mock_app) == []


def test_retry_after_parse_defaults_to_one_second():
    parse = qbo_client_module._retry_after_seconds
    assert parse(httpx.Response(429, headers={"Retry-After": "2"})) == 2.0
    assert parse(httpx.Response(429)) == 1.0
    # RFC 9110 HTTP-date form: unparseable as float — default, don't crash.
    http_date = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert parse(http_date) == 1.0


def test_qbo_error_carries_status_and_message(db_session, seed_six_pdfs, qbo):
    # Exhaust the throttle budget: three one-shot 429 nonces would be needed;
    # simpler — an unknown account is a permanent 400.
    plan = build_journal_entry(db_session, property_id="HISJ", business_date=DAY)
    body = qbo_push.journal_entry_body(plan)
    body["Line"][0]["JournalEntryLineDetail"]["AccountRef"]["value"] = "9999"
    with pytest.raises(QboError) as excinfo:
        qbo.post_journal_entry(body, request_id="qbo-error-test")
    assert excinfo.value.status == 400
    assert "account" in excinfo.value.message.lower()


class _GrantCounter(httpx.BaseTransport):
    """Counts token-grant requests passing through to the mock."""

    def __init__(self, mock_app: Any) -> None:
        self._inner = SyncASGITransport(mock_app)
        self.grants = 0
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/v1/tokens/bearer":
            with self._lock:
                self.grants += 1
        return self._inner.handle_request(request)


def test_concurrent_posts_on_one_client_refresh_exactly_once(mock_app):
    # Two threads race post_journal_entry through ONE shared client (the
    # portal's threadpool scenario). The client's internal lock serializes
    # them: exactly ONE lazy refresh consumes the bootstrap token, and both
    # posts succeed. Without the lock, both threads see a missing access token
    # and refresh with the same rotating token — the loser gets invalid_grant
    # (or, timing depending, a second grant fires), so either assertion below
    # catches the regression.
    transport = _GrantCounter(mock_app)
    client = QboClient(
        "http://mock-qbo",
        "client",
        "secret",
        REALM,
        StaticTokenStore(_bootstrap_refresh_token(mock_app)),
        transport=transport,
    )
    barrier = threading.Barrier(2)
    results: dict[str, str] = {}
    errors: list[BaseException] = []

    def worker(request_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            results[request_id] = client.post_journal_entry(
                BALANCED_JE, request_id=request_id
            )
        except BaseException as exc:  # surfaced below — threads must not die silently
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(f"concurrent-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []  # no invalid_grant on either thread
    assert sorted(results.values()) == ["1", "2"]  # both JEs really posted
    assert transport.grants == 1  # one shared lazy refresh, never a raced second grant
    assert len(_stored_jes(mock_app)) == 2


# --- push_month -------------------------------------------------------------------


def test_push_month_per_date_outcomes(db_session, seed_six_pdfs, mock_app, qbo):
    results = push_month(db_session, qbo, property_id="HISJ", month="2026-07")
    assert [(d, r.status) for d, r in results] == [(DAY, "pushed")]
    assert len(_stored_jes(mock_app)) == 1
    again = push_month(db_session, qbo, property_id="HISJ", month="2026-07")
    assert [(d, r.status) for d, r in again] == [(DAY, "already-pushed")]
    assert push_month(db_session, qbo, property_id="HISJ", month="2026-06") == []


def test_push_month_unmapped_gl_is_independent_failed_outcome(
    db_session, seed_six_pdfs, mock_app, qbo
):
    db_session.execute(
        update(UsaliFinancialFact)
        .where(
            UsaliFinancialFact.property_id == "HISJ",
            UsaliFinancialFact.usali_line_item == "Parking",
        )
        .values(gl_account_code=None)
    )
    db_session.commit()
    results = push_month(db_session, qbo, property_id="HISJ", month="2026-07")
    assert len(results) == 1
    d, result = results[0]
    assert d == DAY and result.status == "failed"
    assert result.message is not None and "Parking" in result.message
    # Nothing was attempted against QBO, so no ledger row exists.
    assert db_session.scalar(select(QboPushLedger)) is None
    assert _stored_jes(mock_app) == []


def test_push_month_missing_coa_account_is_independent_failed_outcome(
    db_session, seed_six_pdfs, mock_app, qbo, monkeypatch
):
    # A GL code that vanished from qbo_accounts.yaml must degrade to a per-date
    # failed outcome, not abort the month and discard collected results.
    real_names = qbo_push._load_account_names
    monkeypatch.setattr(
        qbo_push,
        "_load_account_names",
        lambda path: {k: v for k, v in real_names(path).items() if k != "4000"},
    )
    results = push_month(db_session, qbo, property_id="HISJ", month="2026-07")
    assert len(results) == 1
    d, result = results[0]
    assert d == DAY and result.status == "failed"
    assert result.message is not None and "4000" in result.message
    assert db_session.scalar(select(QboPushLedger)) is None
    assert _stored_jes(mock_app) == []


def test_month_bounds_and_fact_dates(db_session, seed_six_pdfs):
    assert month_bounds("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert month_bounds("2028-02") == (date(2028, 2, 1), date(2028, 2, 29))
    for bad in ("2026-13", "2026-00", "202607", "2026-7", "july"):
        with pytest.raises(ValueError):
            month_bounds(bad)
    assert fact_dates(db_session, property_id="HISJ", month="2026-07") == [DAY]
    assert fact_dates(db_session, property_id="HISJ", month="2026-06") == []


def test_push_ledger_rows_filters(db_session, seed_six_pdfs, qbo):
    push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    push_day(db_session, qbo, property_id="SSSJ", business_date=DAY)
    assert [r.property_id for r in push_ledger_rows(db_session)] == ["HISJ", "SSSJ"]
    assert [r.property_id for r in push_ledger_rows(db_session, property_id="SSSJ")] == ["SSSJ"]
    assert push_ledger_rows(db_session, month="2026-06") == []
    assert len(push_ledger_rows(db_session, property_id="HISJ", month="2026-07")) == 1


# --- CLI --------------------------------------------------------------------------


def test_cli_dry_run_prints_plan_and_writes_nothing(db_session, seed_six_pdfs):
    result = runner.invoke(
        app, ["qbo-push", "--property", "HISJ", "--date", "2026-07-07", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "12,439.66" in result.output
    assert "1210" in result.output and "Guest Ledger Clearing" in result.output
    assert db_session.scalar(select(QboPushLedger)) is None


def test_cli_dry_run_month_mode(db_session, seed_six_pdfs):
    result = runner.invoke(
        app, ["qbo-push", "--property", "SSSJ", "--month", "2026-07", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "7,228.93" in result.output
    assert db_session.scalar(select(QboPushLedger)) is None


def test_cli_qbo_push_requires_exactly_one_of_date_or_month(db_session):
    both = runner.invoke(
        app,
        ["qbo-push", "--property", "HISJ", "--date", "2026-07-07", "--month", "2026-07"],
    )
    assert both.exit_code != 0
    neither = runner.invoke(app, ["qbo-push", "--property", "HISJ"])
    assert neither.exit_code != 0


def test_cli_dry_run_unmapped_gl_fails_listing_lines(db_session, seed_six_pdfs):
    db_session.execute(
        update(UsaliFinancialFact)
        .where(
            UsaliFinancialFact.property_id == "HISJ",
            UsaliFinancialFact.usali_line_item == "Parking",
        )
        .values(gl_account_code=None)
    )
    db_session.commit()
    result = runner.invoke(
        app, ["qbo-push", "--property", "HISJ", "--date", "2026-07-07", "--dry-run"]
    )
    assert result.exit_code == 1
    assert "FAILED" in result.stderr and "Parking" in result.stderr


def test_cli_push_unreachable_qbo_fails_cleanly(db_session, seed_six_pdfs, monkeypatch):
    # Nothing listening on port 1: the CLI must print a FAILED line, not a
    # raw httpx.ConnectError traceback, and record nothing in the ledger.
    monkeypatch.setenv("USALI_QBO_BASE_URL", "http://127.0.0.1:1")
    result = runner.invoke(app, ["qbo-push", "--property", "HISJ", "--date", "2026-07-07"])
    assert result.exit_code == 1
    assert "FAILED" in result.stderr
    assert "cannot reach QBO" in result.stderr and "127.0.0.1:1" in result.stderr
    assert db_session.scalar(select(QboPushLedger)) is None


def test_cli_qbo_status(db_session, seed_six_pdfs, qbo):
    empty = runner.invoke(app, ["qbo-status"])
    assert empty.exit_code == 0
    assert "no QBO pushes recorded" in empty.output

    push_day(db_session, qbo, property_id="HISJ", business_date=DAY)
    result = runner.invoke(app, ["qbo-status", "--property", "HISJ", "--month", "2026-07"])
    assert result.exit_code == 0, result.output
    assert "HISJ" in result.output and "2026-07-07" in result.output
    assert "pushed" in result.output

    bad = runner.invoke(app, ["qbo-status", "--month", "not-a-month"])
    assert bad.exit_code != 0
