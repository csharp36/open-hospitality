"""Pay-run API + the provider seam in create_app (Pillar C2, Task 8).

Seeding reuses tests/test_payroll_run.py's helpers (same _OPENER keypair —
the injected opener must be able to open the seeded profiles). The client
injects `payroll_provider_factory=lambda: provider`; the run's stored
provider NAME still comes from settings ("gusto" by default) — the name is
config, the instance is the seam.

The security test is the point: after a processed 2-employee run, NO response
carries the SSN, bank values, or any per-employee money ("320.00"); only the
department aggregate ("640.00") appears, and only in the detail.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from usali.adp_adapter import AdpAdapter
from usali.db import make_session_factory
from usali.gusto_adapter import GustoAdapter
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import AuditEvent, PayRun
from usali.payroll_provider import InMemoryPayrollProvider
from usali.photo_store import InMemoryPhotoStore
from usali.server import _payroll_provider_from_settings, create_app
from tests.authkit import make_authkit
from tests.grants import grant_role
from tests.test_payroll_run import (
    _OPENER,
    _approved_card,
    _employee,
    _seed,
    _shift,
    _two_employees_16h_each,
)


def _client(db_engine, tmp_path, verifier, provider):
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p", failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=_OPENER,
        payroll_provider_factory=lambda: provider,
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


# --- The config-only provider switch, pinned ---------------------------------


def test_provider_factory_switches_on_settings(monkeypatch):
    monkeypatch.setenv("USALI_PAYROLL_PROVIDER", "gusto")
    assert isinstance(_payroll_provider_from_settings(), GustoAdapter)
    monkeypatch.setenv("USALI_PAYROLL_PROVIDER", "adp")
    assert isinstance(_payroll_provider_from_settings(), AdpAdapter)
    monkeypatch.setenv("USALI_PAYROLL_PROVIDER", "paychex")
    with pytest.raises(RuntimeError, match="paychex"):
        _payroll_provider_from_settings()


def test_create_app_rejects_an_unknown_provider_at_construction(
    db_engine, tmp_path, monkeypatch
):
    """Without an injected factory, a bad USALI_PAYROLL_PROVIDER must fail at
    create_app time (cheap string check) — not lazily on the first pay run."""
    monkeypatch.setenv("USALI_PAYROLL_PROVIDER", "paychex")
    verifier, _ = make_authkit()
    with pytest.raises(RuntimeError, match="paychex"):
        create_app(
            inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
            failed_dir=tmp_path / "f",
            session_factory=make_session_factory(db_engine),
            token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
            photo_store=InMemoryPhotoStore(), opener=_OPENER,
        )
    # An injected factory is unaffected — the seam bypasses the settings check.
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=_OPENER,
        payroll_provider_factory=InMemoryPayrollProvider,
    )
    assert app is not None
