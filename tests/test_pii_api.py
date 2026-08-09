import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.employees import make_employee
from tests.grants import grant_role
from usali.config import Settings
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    EmployeePayrollProfile,
    Organization,
    PaySchedule,
    Property,
)
from usali.opener import SoftwareOpener, seal_for_test
from usali.photo_store import InMemoryPhotoStore
from usali.pii_crypto import SealedEnvelope
from usali.server import _opener_from_settings, create_app
from tests.authkit import make_authkit
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _client(db_engine, tmp_path, verifier, opener):
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p", failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=opener,
    )
    return TestClient(app)


def test_public_key_endpoint_returns_the_injected_pubkey(db_engine, tmp_path):
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    r = c.get("/api/payroll/pii-public-key",
              headers={"Authorization": f"Bearer {mint(roles=['payroll_admin'])}"})
    assert r.status_code == 200
    body = r.json()
    assert body["key_id"] == "dev-1"
    assert body["suite"].startswith("DHKEM-P256")
    assert len(base64.b64decode(body["public_key"])) == 65


def test_public_key_endpoint_reachable_by_a_non_payroll_operator(db_engine, tmp_path):
    # The public key is served to ANY authenticated operator (require_operator),
    # not only payroll_admin — an accountant must be able to fetch it.
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    r = c.get("/api/payroll/pii-public-key",
              headers={"Authorization": f"Bearer {mint(roles=['accountant'])}"})
    assert r.status_code == 200
    assert len(base64.b64decode(r.json()["public_key"])) == 65


def test_public_key_endpoint_rejects_unauthenticated(db_engine, tmp_path):
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, _ = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    assert c.get("/api/payroll/pii-public-key").status_code == 401


# A real 32-byte base64 key so prod Settings pass the symmetric-key fail-fast
# (Task 1) and we can exercise the opener-specific fail-fast in isolation.
_REAL_FIELD_KEY = "bm90LWEtcmVhbC1rZXktYnV0LTMyLWJ5dGVzLWxvbmchIQ=="


def test_prod_without_an_injected_opener_refuses_the_in_process_key():
    # env=prod must NOT silently build a SoftwareOpener from the in-process key.
    settings = Settings(env="prod", field_encryption_key=_REAL_FIELD_KEY)
    with pytest.raises(RuntimeError, match="HSM-backed Opener"):
        _opener_from_settings(settings)


def test_dev_builds_a_software_opener_from_settings():
    opener = _opener_from_settings(Settings(env="dev"))
    assert isinstance(opener, SoftwareOpener)


@pytest.mark.parametrize("env", ["production", "PROD", "prod\n", "staging"])
def test_any_non_dev_env_refuses_the_in_process_opener(env):
    # Fail closed: the opener guard must NOT key off the exact string "prod".
    settings = Settings(env=env, field_encryption_key=_REAL_FIELD_KEY)
    with pytest.raises(RuntimeError, match="HSM-backed Opener"):
        _opener_from_settings(settings)


@pytest.mark.parametrize("env", ["dev", "test", "local"])
def test_known_non_prod_envs_build_a_software_opener(env):
    assert isinstance(_opener_from_settings(Settings(env=env)), SoftwareOpener)


# --- Task 6: blind-overwrite write/status API + pay schedule ------------------


def _seed_property(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(
        Property(property_id="HISJ", org_id=1, name="HISJ", pms_source="OPERA", wage_jurisdiction="US-CA")
    )
    db_session.flush()
    db_session.commit()
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "payroll_admin", sub="pa")
    grant_role(db_session, "accountant", sub="acct")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")


def _seed_employee(db_session):
    _seed_property(db_session)
    emp = make_employee(db_session, property_id="HISJ", full_name="Hank H", pay_type="hourly")
    db_session.add(emp)
    db_session.commit()
    return emp.employee_id


def _sealed(op, emp_id, field, value):
    return seal_for_test(
        op.public_key(), value.encode(), aad=f"{emp_id}:{field}".encode()
    ).to_json()


def test_payroll_admin_writes_sealed_pii_and_it_is_opaque_and_audited(
    db_engine, db_session, tmp_path
):
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.put(
        f"/api/payroll/employees/{emp_id}/profile",
        headers=pa,
        json={
            "ssn": _sealed(op, emp_id, "ssn", "123-45-6789"),
            "bank_account": _sealed(op, emp_id, "bank_account", "000123456"),
            "account_type": "checking",
        },
    )
    assert r.status_code == 200

    # Stored verbatim and OPAQUE — the plaintext SSN is nowhere in the row.
    prof = db_session.execute(select(EmployeePayrollProfile)).scalars().one()
    assert prof.ssn_sealed is not None and "123-45-6789" not in prof.ssn_sealed
    # But it is a real seal: the opener round-trips it with the reconstructed AAD.
    assert (
        op.open(SealedEnvelope.parse(prof.ssn_sealed), aad=f"{emp_id}:ssn".encode())
        == b"123-45-6789"
    )
    # Audited.
    actions = [
        a.action
        for a in db_session.execute(
            select(AuditEvent).where(AuditEvent.resource_id == str(emp_id))
        )
        .scalars()
        .all()
    ]
    assert "write_payroll_pii" in actions


def test_status_returns_flags_only_never_a_value(db_engine, db_session, tmp_path):
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    c.put(
        f"/api/payroll/employees/{emp_id}/profile",
        headers=pa,
        json={"ssn": _sealed(op, emp_id, "ssn", "123-45-6789")},
    )

    r = c.get(f"/api/payroll/employees/{emp_id}/profile", headers=pa)
    assert r.status_code == 200
    body = r.json()
    # bank_account / bank_routing / account_type left this surface in E5 —
    # the deposit destination is a chain on /deposit-accounts now.
    assert body == {
        "employee_id": emp_id,
        "ssn_on_file": True,
        "tax_elections_on_file": False,
    }
    # No sealed value or plaintext anywhere in the response.
    assert "sealed" not in r.text and "123-45-6789" not in r.text and "enc" not in body


def test_status_read_is_audited(db_engine, db_session, tmp_path):
    # The AuditEvent contract says a PII read is logged. A status read writes a
    # `read_payroll_pii_status` row scoped by resource_id ONLY (never a value).
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    r = c.get(f"/api/payroll/employees/{emp_id}/profile", headers=pa)
    assert r.status_code == 200

    events = (
        db_session.execute(
            select(AuditEvent).where(AuditEvent.action == "read_payroll_pii_status")
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.actor_subject == "pa"
    assert ev.resource_type == "employee"
    assert ev.resource_id == str(emp_id)


def test_account_type_out_of_range_is_422_and_stores_nothing(
    db_engine, db_session, tmp_path
):
    # account_type is constrained to {checking, savings}; a bad/oversized value
    # is rejected at the API boundary (422), never a DB 500.
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    # account_type moved to the deposit-accounts surface in E5; the boundary
    # constraint moved with it (DepositAccountBody's Literal).
    r = c.put(
        f"/api/payroll/employees/{emp_id}/deposit-accounts",
        headers=pa,
        json={"accounts": [{
            "allocation_type": "remainder", "allocation_value": None,
            "account_type": "wire-transfer-and-way-too-long",
            "sealed_account": _sealed(op, emp_id, "x", "0"),
            "sealed_routing": _sealed(op, emp_id, "y", "0"),
        }]},
    )
    assert r.status_code == 422
    from usali.models import DepositAccount
    assert db_session.execute(select(DepositAccount)).scalars().all() == []


def test_status_for_an_employee_with_no_profile_is_all_false(
    db_engine, db_session, tmp_path
):
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    r = c.get(f"/api/payroll/employees/{emp_id}/profile", headers=pa)
    assert r.status_code == 200
    assert r.json() == {
        "employee_id": emp_id,
        "ssn_on_file": False,
        "tax_elections_on_file": False,
    }


def test_non_payroll_role_cannot_read_or_write(db_engine, db_session, tmp_path):
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    gm = {
        "Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"
    }
    assert (
        c.get(f"/api/payroll/employees/{emp_id}/profile", headers=gm).status_code == 403
    )
    assert (
        c.put(
            f"/api/payroll/employees/{emp_id}/profile",
            headers=gm,
            json={"account_type": "checking"},
        ).status_code
        == 403
    )
    # An accountant (global-property VIEW role) is likewise NOT a payroll admin.
    acct = {"Authorization": f"Bearer {mint(roles=['accountant'], sub='acct')}"}
    assert (
        c.get(f"/api/payroll/employees/{emp_id}/profile", headers=acct).status_code
        == 403
    )
    # Nothing was ever written by the rejected callers.
    assert db_session.execute(select(EmployeePayrollProfile)).scalars().all() == []


def test_malformed_envelope_is_422_and_stores_nothing(db_engine, db_session, tmp_path):
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    r = c.put(
        f"/api/payroll/employees/{emp_id}/profile",
        headers=pa,
        json={"ssn": "{not a seal"},
    )
    assert r.status_code == 422
    assert db_session.execute(select(EmployeePayrollProfile)).scalars().all() == []


def test_one_malformed_field_rolls_back_the_whole_write(
    db_engine, db_session, tmp_path
):
    # All-or-nothing: a good ssn + a bad tax_elections must write NEITHER.
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    r = c.put(
        f"/api/payroll/employees/{emp_id}/profile",
        headers=pa,
        json={
            "ssn": _sealed(op, emp_id, "ssn", "123-45-6789"),
            "tax_elections": "{not a seal",
        },
    )
    assert r.status_code == 422
    assert db_session.execute(select(EmployeePayrollProfile)).scalars().all() == []


def test_envelope_sealed_to_a_foreign_key_id_is_rejected(
    db_engine, db_session, tmp_path
):
    # Adversarial: a caller seals to an attacker key_id the server can't open.
    # Structure is valid, but key_id != current → 422, and nothing is stored.
    emp_id = _seed_employee(db_session)
    server_op = SoftwareOpener.generate(key_id="dev-1")
    attacker_op = SoftwareOpener.generate(key_id="attacker-key")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, server_op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    foreign = seal_for_test(
        attacker_op.public_key(), b"123-45-6789", aad=f"{emp_id}:ssn".encode()
    ).to_json()
    r = c.put(
        f"/api/payroll/employees/{emp_id}/profile", headers=pa, json={"ssn": foreign}
    )
    assert r.status_code == 422
    assert "key_id" in r.json()["detail"]
    assert db_session.execute(select(EmployeePayrollProfile)).scalars().all() == []


def test_write_to_unknown_employee_is_404(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    db_session.commit()
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    r = c.put(
        "/api/payroll/employees/9999/profile",
        headers=pa,
        json={"ssn": _sealed(op, 9999, "ssn", "123-45-6789")},
    )
    assert r.status_code == 404
    assert db_session.execute(select(EmployeePayrollProfile)).scalars().all() == []


def test_second_write_overwrites_the_first_blindly(db_engine, db_session, tmp_path):
    emp_id = _seed_employee(db_session)
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    c.put(
        f"/api/payroll/employees/{emp_id}/profile",
        headers=pa,
        json={"ssn": _sealed(op, emp_id, "ssn", "111-11-1111")},
    )
    c.put(
        f"/api/payroll/employees/{emp_id}/profile",
        headers=pa,
        json={"ssn": _sealed(op, emp_id, "ssn", "222-22-2222")},
    )
    db_session.expire_all()
    prof = db_session.execute(select(EmployeePayrollProfile)).scalars().one()
    assert (
        op.open(SealedEnvelope.parse(prof.ssn_sealed), aad=f"{emp_id}:ssn".encode())
        == b"222-22-2222"
    )


# --- pay schedule -------------------------------------------------------------


def test_payroll_admin_round_trips_a_pay_schedule(db_engine, db_session, tmp_path):
    _seed_property(db_session)
    db_session.commit()
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    body = {
        "frequency": "biweekly",
        "anchor": "2026-01-05",
        "check_date_offset_days": 5,
    }
    r = c.put("/api/payroll/properties/HISJ/pay-schedule", headers=pa, json=body)
    assert r.status_code == 200
    got = c.get("/api/payroll/properties/HISJ/pay-schedule", headers=pa)
    assert got.status_code == 200
    assert got.json() == {
        "property_id": "HISJ",
        "frequency": "biweekly",
        "anchor": "2026-01-05",
        "check_date_offset_days": 5,
    }
    # Exactly one row — a second PUT updates in place, not append.
    c.put(
        "/api/payroll/properties/HISJ/pay-schedule",
        headers=pa,
        json={"frequency": "weekly", "anchor": "2026-01-05", "check_date_offset_days": 3},
    )
    db_session.expire_all()
    rows = db_session.execute(select(PaySchedule)).scalars().all()
    assert len(rows) == 1 and rows[0].frequency == "weekly"


def test_pay_schedule_anchor_must_align_with_the_platform_grid(
    db_engine, db_session, tmp_path
):
    """The anchor is display/config metadata constrained to the platform's
    biweekly payroll grid (settings.payroll_period_anchor, default 2026-01-05):
    aligned = a multiple of 14 days away. A misaligned anchor would silently
    diverge from every period computation on the money path — 422, store nothing."""
    _seed_property(db_session)
    db_session.commit()
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    pa = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}

    # Misaligned (global + 3d): 422, and NOTHING is stored.
    r = c.put(
        "/api/payroll/properties/HISJ/pay-schedule",
        headers=pa,
        json={"frequency": "biweekly", "anchor": "2026-01-08",
              "check_date_offset_days": 5},
    )
    assert r.status_code == 422
    assert "grid" in r.json()["detail"]
    assert db_session.execute(select(PaySchedule)).scalars().all() == []
    assert c.get("/api/payroll/properties/HISJ/pay-schedule",
                 headers=pa).status_code == 404

    # Aligned (global + 14d): accepted.
    r = c.put(
        "/api/payroll/properties/HISJ/pay-schedule",
        headers=pa,
        json={"frequency": "biweekly", "anchor": "2026-01-19",
              "check_date_offset_days": 5},
    )
    assert r.status_code == 200
    assert r.json()["anchor"] == "2026-01-19"

    # A misaligned UPDATE of the existing row is also rejected — unchanged.
    r = c.put(
        "/api/payroll/properties/HISJ/pay-schedule",
        headers=pa,
        json={"frequency": "biweekly", "anchor": "2026-01-20",
              "check_date_offset_days": 5},
    )
    assert r.status_code == 422
    db_session.expire_all()
    [row] = db_session.execute(select(PaySchedule)).scalars().all()
    assert row.anchor.isoformat() == "2026-01-19"


def test_non_payroll_role_cannot_touch_the_pay_schedule(
    db_engine, db_session, tmp_path
):
    _seed_property(db_session)
    db_session.commit()
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm')}"}
    assert (
        c.get("/api/payroll/properties/HISJ/pay-schedule", headers=gm).status_code
        == 403
    )
    assert (
        c.put(
            "/api/payroll/properties/HISJ/pay-schedule",
            headers=gm,
            json={"frequency": "weekly", "anchor": "2026-01-05"},
        ).status_code
        == 403
    )
