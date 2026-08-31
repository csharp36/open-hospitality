"""Pay-run API + the provider seam in create_app (Pillar C2, Task 8).

Seeding reuses tests/test_payroll_run.py's helpers (same _OPENER keypair —
the injected opener must be able to open the seeded profiles). The client
injects `payroll_provider_factory=lambda _factory: provider`; the seam takes
the REQUEST'S org-bound session factory since OH-17, because the real
resolver reads the active org's `org_integration_credential` row.

The run's stored provider NAME still comes from `settings.payroll_provider`
("gusto" by default) — the name is config, the instance is the seam. That
split is a KNOWN OH-17 loose end, flagged at `payroll_run_api.create_run`:
it is harmless only because org 1's row is itself seeded from that same env
(so name and row agree), and an org with no row 503s before ever reaching
the run. The connect UI is what makes them divergable, and is where it must
be fixed — do not "tidy" the name onto the row here without reading that
comment first: `provider_name` is also the IDENTITY KEY of
`ProviderEmployeeRef`.

The security test is the point: after a processed 2-employee run, NO response
carries the SSN, bank values, or any per-employee money ("320.00"); only the
department aggregate ("640.00") appears, and only in the detail.
"""

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from usali.db import make_session_factory
from usali.integrations import ResolvedPayroll
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import AuditEvent, PayRun, ProviderEmployeeRef
from usali.payroll_provider import InMemoryPayrollProvider
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.tenancy import ORG_INFO_KEY
from tests.authkit import make_authkit
from tests.credentials import plant_credential, unreadable_ciphertext
from tests.grants import grant_role
from tests.test_payroll_run import (
    _OPENER,
    _approved_card,
    _employee,
    _seed,
    _shift,
    _two_employees_16h_each,
)


def _client(db_engine, tmp_path, verifier, provider, provider_name="gusto"):
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p", failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=_OPENER,
        # OH-17: the seam is handed the request's org-bound session factory
        # (the real resolver reads the tenant's credential row from it). The
        # fake ignores it — see `test_the_seam_is_handed_the_requests_org_
        # bound_factory` for the pin that it is the REQUEST's factory and not
        # the app's unbound base one.
        payroll_provider_factory=lambda _factory: ResolvedPayroll(provider_name, provider),
    )
    return TestClient(app)


_BODY = {"property": "HISJ", "in_period": "2026-07-06"}


def test_post_run_executes_and_get_shows_it(db_engine, db_session, tmp_path):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "submitted"
    # The stored provider NAME is the settings value even with an injected
    # instance — the name is config, the instance is the test seam.
    assert body["provider"] == "gusto"
    assert body["period_start"] == "2026-07-06"
    assert body["period_end"] == "2026-07-19"
    assert body["check_date"] == "2026-07-24"  # period_end + offset 5
    run_id = body["pay_run_id"]

    listed = c.get("/api/payroll/runs", params={"property": "HISJ"}, headers=pa)
    assert listed.status_code == 200
    assert [row["pay_run_id"] for row in listed.json()] == [run_id]
    assert listed.json()[0]["status"] == "submitted"

    fetched = c.post(f"/api/payroll/runs/{run_id}/fetch-results", headers=pa)
    assert fetched.status_code == 200
    assert fetched.json() == {"status": "processed", "lines": 2}

    detail = c.get(f"/api/payroll/runs/{run_id}", headers=pa)
    assert detail.status_code == 200
    d = detail.json()
    assert d["status"] == "processed"
    assert d["failure_reason"] is None
    # ONE department aggregate — period-grain, name-resolved, NO per-employee money.
    [agg] = d["department_aggregates"]
    assert agg["department"] == "Housekeeping"
    assert Decimal(agg["gross"]) == Decimal("640.00")
    assert Decimal(agg["employer_burden"]) == Decimal("64.00")
    assert Decimal(agg["hours"]) == Decimal("32.00")


def test_preflight_blockers_are_a_422_with_names(db_engine, db_session, tmp_path):
    dept_id, pos_id, device_id = _seed(db_session)
    emp_id = _employee(db_session, dept_id, pos_id, name="Norah NoRate", pay_rate=None)
    _shift(db_session, device_id, emp_id, 6, 9, 17)
    db_session.commit()
    _approved_card(db_session, emp_id)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "no pay rate on file" in detail
    assert "Norah NoRate" in detail
    # NOTHING created, NO provider call.
    assert db_session.execute(select(PayRun)).scalars().all() == []
    assert provider._employees == []


def test_non_payroll_admin_is_403(db_engine, db_session, tmp_path):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}

    assert c.post("/api/payroll/runs", headers=acct, json=_BODY).status_code == 403
    assert c.get("/api/payroll/runs", params={"property": "HISJ"},
                 headers=acct).status_code == 403
    assert c.get("/api/payroll/runs/1", headers=acct).status_code == 403
    assert c.post("/api/payroll/runs/1/fetch-results", headers=acct).status_code == 403
    # And no operator token at all is a 401 at the outer gate.
    assert c.post("/api/payroll/runs", json=_BODY).status_code == 401
    assert provider._employees == []


def test_duplicate_period_is_409(db_engine, db_session, tmp_path):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    assert c.post("/api/payroll/runs", headers=pa, json=_BODY).status_code == 201
    dup = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert dup.status_code == 409
    assert "already exists" in dup.json()["detail"]
    assert len(db_session.execute(select(PayRun)).scalars().all()) == 1


def test_fetch_results_on_a_draft_or_failed_run_is_409(db_engine, db_session, tmp_path):
    _two_employees_16h_each(db_session)

    class _FailingSubmit(InMemoryPayrollProvider):
        def submit_pay_run(self, *, period_start, period_end, check_date, entries):
            from usali.payroll_provider import ProviderError
            raise ProviderError("mem payroll submit failed (500): internal error")

    provider = _FailingSubmit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["status"] == "failed"
    assert "500" in detail["failure_reason"]
    run_id = detail["pay_run_id"]
    # The failed run row IS persisted (so a re-POST can replace it)...
    [row] = db_session.execute(select(PayRun)).scalars().all()
    assert row.pay_run_id == run_id and row.status == "failed"
    # ...but its results cannot be fetched.
    f = c.post(f"/api/payroll/runs/{run_id}/fetch-results", headers=pa)
    assert f.status_code == 409
    assert "failed" in f.json()["detail"]
    # An unknown run is a 404, not a 409.
    assert c.post("/api/payroll/runs/9999/fetch-results", headers=pa).status_code == 404


def test_sync_failure_is_a_502_not_a_500(db_engine, db_session, tmp_path):
    """A ProviderError raised during employee SYNC (before submit) must follow
    the same handled path as a submit failure: 502 with the persisted failed
    run, never an uncaught 500."""
    _two_employees_16h_each(db_session)

    class _FailingSync(InMemoryPayrollProvider):
        def sync_employee(self, employee):
            from usali.payroll_provider import ProviderError
            raise ProviderError("mem employee sync failed (422)")

    provider = _FailingSync()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail["status"] == "failed"
    assert "422" in detail["failure_reason"]
    # The failed run row IS persisted (so a re-POST can replace it).
    [row] = db_session.execute(select(PayRun)).scalars().all()
    assert row.pay_run_id == detail["pay_run_id"] and row.status == "failed"


def test_responses_never_carry_pii_or_per_employee_money(db_engine, db_session, tmp_path):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    created = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    run_id = created.json()["pay_run_id"]
    fetched = c.post(f"/api/payroll/runs/{run_id}/fetch-results", headers=pa)
    listed = c.get("/api/payroll/runs", params={"property": "HISJ"}, headers=pa)
    detail = c.get(f"/api/payroll/runs/{run_id}", headers=pa)
    assert detail.json()["status"] == "processed"

    for resp in (created, fetched, listed, detail):
        # The seeded PII values (the provider DID receive them in plaintext).
        assert "123-45-6789" not in resp.text
        assert "000123456" not in resp.text
        assert "021000021" not in resp.text
        # Per-employee money: each employee grossed $320.00 — never in a response.
        assert "320.00" not in resp.text
    # The DEPARTMENT aggregate is what the API serves, in the detail only.
    assert "640.00" in detail.text
    for resp in (created, fetched, listed):
        assert "640.00" not in resp.text


# --- C3: the audited per-employee lines endpoint -----------------------------


def _lines_audits(db_session):
    return db_session.execute(
        select(AuditEvent).where(AuditEvent.action == "read_pay_run_lines")
    ).scalars().all()


def test_lines_endpoint_returns_decrypted_money_and_audits(db_engine, db_session, tmp_path):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    run_id = c.post("/api/payroll/runs", headers=pa, json=_BODY).json()["pay_run_id"]
    assert c.post(f"/api/payroll/runs/{run_id}/fetch-results", headers=pa).json() == {
        "status": "processed", "lines": 2,
    }

    r = c.get(f"/api/payroll/runs/{run_id}/lines", headers=pa)
    assert r.status_code == 200
    body = r.json()
    assert body["pay_run_id"] == run_id
    assert body["status"] == "processed"
    lines = body["lines"]
    assert len(lines) == 2
    assert [row["employee_name"] for row in lines] == ["Hank H", "Ivy I"]
    assert lines[0]["hours"] == "16.00"
    assert lines[0]["gross"] == "320.00"           # decrypted decimal string
    assert lines[0]["employee_taxes"] == "48.00"
    assert lines[0]["employer_taxes"] == "32.00"
    assert lines[0]["net"] == "272.00"
    # Nothing beyond the intended fields (no SSN, no bank, no rate).
    assert set(lines[0]) == {
        "employee_id", "employee_name", "hours", "gross",
        "employee_taxes", "employer_taxes", "net",
    }
    # AUDITED: one read_pay_run_lines row per request, resource_id = the run id.
    audits = _lines_audits(db_session)
    assert len(audits) == 1
    assert audits[0].resource_id == str(run_id)
    assert audits[0].resource_type == "pay_run"
    assert audits[0].actor_subject == "pa"
    # A second request audits again (every read).
    assert c.get(f"/api/payroll/runs/{run_id}/lines", headers=pa).status_code == 200
    assert len(_lines_audits(db_session)) == 2


def test_lines_endpoint_is_payroll_admin_only(db_engine, db_session, tmp_path):
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    run_id = c.post("/api/payroll/runs", headers=pa, json=_BODY).json()["pay_run_id"]
    c.post(f"/api/payroll/runs/{run_id}/fetch-results", headers=pa)

    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm')}"}
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}
    assert c.get(f"/api/payroll/runs/{run_id}/lines", headers=gm).status_code == 403
    assert c.get(f"/api/payroll/runs/{run_id}/lines", headers=acct).status_code == 403
    # No token at all is a 401 at the outer gate.
    assert c.get(f"/api/payroll/runs/{run_id}/lines").status_code == 401
    # NO audit row for a denial (the B2 photo-gate convention: the trail
    # records actual egress, not attempts).
    assert _lines_audits(db_session) == []


def test_lines_on_unknown_run_is_404(db_engine, db_session, founding_org, tmp_path):
    grant_role(db_session, "payroll_admin", sub="pa")  # L4: DB-backed authority
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.get("/api/payroll/runs/9999/lines", headers=pa)
    assert r.status_code == 404
    assert r.json()["detail"] == "pay run not found"
    # No egress happened, so no audit row.
    assert _lines_audits(db_session) == []


def test_lines_on_a_submitted_run_show_placeholder_money(db_engine, db_session, tmp_path):
    # Before fetch-results: hours real, money "0" — the run status tells the story.
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, provider)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    run_id = c.post("/api/payroll/runs", headers=pa, json=_BODY).json()["pay_run_id"]

    r = c.get(f"/api/payroll/runs/{run_id}/lines", headers=pa)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "submitted"
    assert len(body["lines"]) == 2
    for row in body["lines"]:
        assert row["hours"] == "16.00"
        assert row["gross"] == "0"
        assert row["employee_taxes"] == "0"
        assert row["employer_taxes"] == "0"
        assert row["net"] == "0"
    # The placeholder read is still an audited read.
    assert len(_lines_audits(db_session)) == 1


# --- The per-TENANT provider seam, pinned ------------------------------------
#
# `USALI_PAYROLL_PROVIDER` used to be the switch, and two tests lived here for
# it: one over `server._payroll_provider_from_settings`, one over `create_app`'s
# fail-fast on a misspelled name. OH-17 deleted both functions. The provider is
# the active org's `org_integration_credential` row now, so there is nothing
# process-wide left to validate at construction — refusing to BOOT on a value
# no request path reads would refuse to boot on a dead letter. Adapter selection
# from a row is pinned in tests/test_integrations.py; what belongs HERE is what
# the API does when the row is missing, and which factory the seam is handed.


def _unconnected_client(db_engine, tmp_path, verifier):
    """A serving app with NO injected provider factory — the real resolver,
    over a world (`_seed`) that plants no payroll credential row."""
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=_OPENER,
    )
    return TestClient(app)


def test_an_unconnected_tenant_gets_a_503_naming_the_connect_surface(
    db_engine, db_session, tmp_path
):
    """ADR-010: a missing credential REFUSES, loudly and by name — it never
    falls back to the process-wide env (which still says "gusto" here, the
    Settings default) and never silently no-ops a pay run.

    The 503 must survive as a 503: the request carries a real payroll_admin
    grant against a seeded property, so nothing earlier in the chain (401,
    403, 404, the 422 preflight) can short-circuit it. Assert the body, not
    just the code — a 503 from somewhere else would pass a bare code check."""
    _two_employees_16h_each(db_session)
    verifier, mint = make_authkit()
    c = _unconnected_client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "/integrations" in detail
    assert "USALI_PAYROLL_PROVIDER" not in detail
    # Nothing was written: a refused run is not a draft row left behind.
    assert db_session.execute(select(PayRun)).scalars().all() == []


def test_an_unreadable_payroll_credential_gets_its_own_named_503(
    db_engine, db_session, tmp_path
):
    """ADR-005: the tenant IS connected to Gusto — the row is there, the token
    was accepted at connect time — but `field_encryption_key` has been rotated
    since, so nothing can read it. A different refusal from the one above, on
    purpose: telling this operator to "connect Gusto or ADP" would have them
    re-enter credentials to fix a key rotation, and the pay run would fail the
    same way the next period.

    Still a 503 and still before any write: a pay run must not half-run into
    an adapter built from a credential nobody could decrypt."""
    _two_employees_16h_each(db_session)
    plant_credential(db_session, "payroll", "gusto", company_id="co-1",
                     api_token=unreadable_ciphertext("gusto-token"))
    verifier, mint = make_authkit()
    c = _unconnected_client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "payroll" in detail
    assert "decrypted" in detail
    assert "not connected" not in detail
    assert "gusto-token" not in detail
    assert db_session.execute(select(PayRun)).scalars().all() == []


def test_the_seam_is_handed_the_requests_org_bound_factory(
    db_engine, db_session, tmp_path
):
    """The seam gets the REQUEST's org-bound factory, not `create_app`'s
    unbound base one. This is the whole tenancy story of OH-17 in one
    assertion: the resolver reads a credential row through whatever it is
    handed, so an unbound factory here would read SOME org's row — under a
    superuser connection (which the suite runs as) RLS does not save you,
    and a second tenant's pay run would be built from the first's
    credentials. `ORG_INFO_KEY` on the session is the L2 write wall's
    binding, so checking it checks the same thing the wall does."""
    _two_employees_16h_each(db_session)
    verifier, mint = make_authkit()
    seen: list[object] = []

    def capturing_factory(factory):
        seen.append(factory)
        return ResolvedPayroll("gusto", InMemoryPayrollProvider())

    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=_OPENER,
        payroll_provider_factory=capturing_factory,
    )
    c = TestClient(app)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    assert c.post("/api/payroll/runs", headers=pa, json=_BODY).status_code == 201

    assert len(seen) == 1
    with seen[0]() as session:  # type: ignore[operator]
        assert session.info[ORG_INFO_KEY] == 1


def test_the_run_records_the_provider_from_the_row_not_the_env(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The stored `pay_run.provider` is the name the ADAPTER was built from,
    never `settings.payroll_provider`.

    This is a mis-PAY guard, not a labelling nicety. `provider_name` is the
    lookup key of `ProviderEmployeeRef` (payroll_run.sync_employees), and
    those refs become `PayRunEntry.provider_employee_id` on submission. Key
    ADP-side ids under "gusto" and a later switch to Gusto finds them "fresh"
    and submits ADP ids to Gusto.

    Env says gusto (the Settings default, set explicitly here); the resolved
    provider says adp. The row must win — in the PayRun and in the ref."""
    monkeypatch.setenv("USALI_PAYROLL_PROVIDER", "gusto")
    _two_employees_16h_each(db_session)
    verifier, mint = make_authkit()
    provider = InMemoryPayrollProvider()
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=_OPENER,
        payroll_provider_factory=lambda _f: ResolvedPayroll("adp", provider),
    )
    c = TestClient(app)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs", headers=pa, json=_BODY)
    assert r.status_code == 201, r.text
    assert r.json()["provider"] == "adp"

    run = db_session.execute(select(PayRun)).scalar_one()
    assert run.provider == "adp"
    # The refs the submission was keyed on carry the same name — this is the
    # half that actually pays the wrong person if it drifts.
    refs = db_session.execute(select(ProviderEmployeeRef)).scalars().all()
    assert refs and {ref.provider for ref in refs} == {"adp"}


def test_an_unknown_run_404s_before_the_connectivity_check(
    db_engine, db_session, tmp_path
):
    """`fetch_results` resolves the provider AFTER its 404/409, so a poll for a
    run that does not exist is answered as "not found" and not as a 503 about
    the tenant's payroll connection.

    The tenant here is genuinely unconnected (no credential row), so the 503 is
    live and would fire the moment the resolution moved above the 404 — which
    is exactly what hoisting the call, or restoring a `Depends`, would do.
    `create_run`'s ordering is deliberately the opposite and is pinned by
    `test_an_unconnected_tenant_gets_a_503_naming_the_connect_surface`."""
    _two_employees_16h_each(db_session)
    verifier, mint = make_authkit()
    c = _unconnected_client(db_engine, tmp_path, verifier)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.post("/api/payroll/runs/9999/fetch-results", headers=pa)
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "pay run not found"


def test_fetching_results_refuses_when_the_tenant_reconnected_a_different_provider(
    db_engine, db_session, tmp_path
):
    """OH-17: `provider_run_id` lives in the SUBMITTING provider's namespace,
    and `fetch_pay_run_results` keys its employee-ref map on `run.provider`.
    Asking whoever is connected NOW for that id is a category error — it
    reaches the wrong provider's API with the wrong id, and the ref lookup
    misses every line.

    Reachable only because OH-17 made the provider a per-tenant runtime
    choice: before it, changing provider meant a redeploy. Found in review
    2026-08-31 — the handler took `_provider(request).adapter` and dropped the
    name, three lines below the docstring saying an adapter separated from its
    name is a mis-pay waiting to happen."""
    _two_employees_16h_each(db_session)
    provider = InMemoryPayrollProvider()
    verifier, mint = make_authkit()
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    submitted = _client(db_engine, tmp_path, verifier, provider).post(
        "/api/payroll/runs", headers=pa, json=_BODY
    )
    assert submitted.status_code == 201
    run_id = submitted.json()["pay_run_id"]
    assert submitted.json()["provider"] == "gusto"

    # Same run, same adapter instance — only the tenant's CONNECTED provider
    # name differs, which is precisely the state a reconnect leaves behind.
    switched = _client(db_engine, tmp_path, verifier, provider, provider_name="adp")
    refused = switched.post(f"/api/payroll/runs/{run_id}/fetch-results", headers=pa)
    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "gusto" in detail and "adp" in detail

    # And the refusal is specific to the mismatch, not a broken fetch path:
    # the still-connected provider fetches the same run fine.
    ok = _client(db_engine, tmp_path, verifier, provider).post(
        f"/api/payroll/runs/{run_id}/fetch-results", headers=pa
    )
    assert ok.status_code == 200
    assert ok.json() == {"status": "processed", "lines": 2}
