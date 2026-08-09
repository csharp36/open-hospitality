"""E5 Task 3: the deposit-account vault surface.

C1's rules carry over verbatim: envelopes validated for STRUCTURE only and
never opened; writes are blind; reads return booleans + allocation metadata,
never a sealed value; everything payroll-admin-gated and audited. What is new
is the SHAPE — a chain replaces the scalar pair, writes are full-replace of
the whole chain (per-row patching invites half-updated chains where account
2 is new and account 3 points at a closed account), and ordinals are
assigned server-side from array position so gaps and duplicates are
unrepresentable at the door.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import (
    AuditEvent,
    DepositAccount,
    Organization,
    Property,
)
from usali.opener import SoftwareOpener, seal_for_test
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS


def _client(db_engine, tmp_path, verifier, opener):
    app = create_app(
        inbox_dir=tmp_path / "in", processed_dir=tmp_path / "p",
        failed_dir=tmp_path / "f",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(), opener=opener,
    )
    return TestClient(app)


def _seed(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ", full_name="Wanda W",
                        pay_type="hourly")
    db_session.commit()
    # L4: role authority is DB grants, not token roles.
    grant_role(db_session, "payroll_admin", sub="pa")
    grant_role(db_session, "property_gm", sub="gm", property_id="HISJ")
    return emp.employee_id


def _sealed(op, emp_id, ordinal, field, plaintext):
    """Seal under the NEW per-ordinal slot aad — the contract with
    sync_employees."""
    return seal_for_test(
        op.public_key(), plaintext,
        aad=f"{emp_id}:deposit:{ordinal}:{field}".encode(),
    ).to_json()


def _account(op, emp_id, ordinal, *, allocation_type="remainder",
             allocation_value=None, account_type="checking"):
    return {
        "allocation_type": allocation_type,
        "allocation_value": allocation_value,
        "account_type": account_type,
        "sealed_account": _sealed(op, emp_id, ordinal, "account", b"000123456"),
        "sealed_routing": _sealed(op, emp_id, ordinal, "routing", b"021000021"),
    }


def _setup(db_engine, db_session, tmp_path):
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    hdr = {"Authorization": f"Bearer {mint(roles=['payroll_admin'], sub='pa')}"}
    emp_id = _seed(db_session)
    return op, c, hdr, emp_id


def test_put_a_two_account_chain_and_read_its_status(db_engine, db_session, tmp_path):
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    body = {"accounts": [
        _account(op, emp_id, 1, allocation_type="amount",
                 allocation_value="50.00", account_type="savings"),
        _account(op, emp_id, 2),
    ]}
    r = c.put(f"/api/payroll/employees/{emp_id}/deposit-accounts",
              json=body, headers=hdr)
    assert r.status_code == 200, r.text

    r = c.get(f"/api/payroll/employees/{emp_id}/deposit-accounts", headers=hdr)
    assert r.status_code == 200
    accounts = r.json()["accounts"]
    assert [a["ordinal"] for a in accounts] == [1, 2]
    assert accounts[0]["allocation_type"] == "amount"
    assert accounts[0]["allocation_value"] == "50.00"
    assert accounts[0]["account_type"] == "savings"
    assert accounts[0]["account_on_file"] is True
    assert accounts[0]["routing_on_file"] is True
    assert accounts[1]["allocation_type"] == "remainder"
    assert accounts[1]["allocation_value"] is None
    # NEVER a sealed value in a status read — the vault has no read path.
    assert "sealed" not in r.text and "enc" not in r.text.lower()
    rows = db_session.execute(select(DepositAccount)).scalars().all()
    assert len(rows) == 2 and all(row.legacy_sealed is False for row in rows)


def test_put_replaces_the_whole_chain_atomically(db_engine, db_session, tmp_path):
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    put = f"/api/payroll/employees/{emp_id}/deposit-accounts"
    assert c.put(put, json={"accounts": [_account(op, emp_id, 1)]},
                 headers=hdr).status_code == 200
    assert c.put(put, json={"accounts": [
        _account(op, emp_id, 1, allocation_type="percent",
                 allocation_value="20.00"),
        _account(op, emp_id, 2),
    ]}, headers=hdr).status_code == 200
    db_session.expire_all()
    rows = db_session.execute(
        select(DepositAccount).order_by(DepositAccount.ordinal)
    ).scalars().all()
    assert [(r.ordinal, r.allocation_type) for r in rows] == [
        (1, "percent"), (2, "remainder"),
    ]


def test_a_malformed_envelope_anywhere_stores_nothing(db_engine, db_session, tmp_path):
    """All-or-nothing, validated BEFORE any write: a bad second account must
    not leave a half-replaced chain — the existing chain survives intact."""
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    put = f"/api/payroll/employees/{emp_id}/deposit-accounts"
    original = {"accounts": [_account(op, emp_id, 1)]}
    assert c.put(put, json=original, headers=hdr).status_code == 200

    bad = {"accounts": [
        _account(op, emp_id, 1, allocation_type="amount",
                 allocation_value="50.00"),
        {**_account(op, emp_id, 2), "sealed_account": "not an envelope"},
    ]}
    r = c.put(put, json=bad, headers=hdr)
    assert r.status_code == 422
    assert "not an envelope" not in r.text  # never echo the submitted value
    db_session.expire_all()
    rows = db_session.execute(select(DepositAccount)).scalars().all()
    assert len(rows) == 1 and rows[0].allocation_type == "remainder", (
        "the original chain must survive a refused replacement untouched"
    )


def test_an_envelope_sealed_to_a_foreign_key_is_refused(db_engine, db_session, tmp_path):
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    foreign = SoftwareOpener.generate(key_id="attacker-1")
    body = {"accounts": [{
        "allocation_type": "remainder", "allocation_value": None,
        "account_type": "checking",
        "sealed_account": seal_for_test(
            foreign.public_key(), b"000123456",
            aad=f"{emp_id}:deposit:1:account".encode()).to_json(),
        "sealed_routing": _sealed(op, emp_id, 1, "routing", b"021000021"),
    }]}
    r = c.put(f"/api/payroll/employees/{emp_id}/deposit-accounts",
              json=body, headers=hdr)
    assert r.status_code == 422
    assert "attacker-1" in r.text  # the key_id is not secret; the value is
    assert db_session.execute(select(DepositAccount)).scalars().all() == []


def test_an_illegal_allocation_chain_is_refused_before_any_write(
    db_engine, db_session, tmp_path
):
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    r = c.put(f"/api/payroll/employees/{emp_id}/deposit-accounts",
              json={"accounts": [
                  _account(op, emp_id, 1),
                  _account(op, emp_id, 2),
              ]}, headers=hdr)
    assert r.status_code == 422
    assert "remainder" in r.json()["detail"]
    assert db_session.execute(select(DepositAccount)).scalars().all() == []


def test_both_directions_are_audited(db_engine, db_session, tmp_path):
    """The C1 contract: even boolean status reads leave an audit row, and
    audit rows carry the employee id ONLY — no values."""
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    put = f"/api/payroll/employees/{emp_id}/deposit-accounts"
    c.put(put, json={"accounts": [_account(op, emp_id, 1)]}, headers=hdr)
    c.get(put, headers=hdr)
    db_session.expire_all()
    actions = [
        (a.action, a.resource_id)
        for a in db_session.execute(select(AuditEvent)).scalars()
        if "deposit" in a.action
    ]
    assert (("write_deposit_accounts", str(emp_id)) in actions)
    assert (("read_deposit_accounts_status", str(emp_id)) in actions)


def test_a_value_the_column_would_round_is_refused_not_rewritten(
    db_engine, db_session, tmp_path
):
    """The review reproduced: percent 99.999 accepted as legal, SILENTLY
    stored as 100.00 by Numeric(10,2), then refused by preflight — the one
    shared predicate judged a value the database never kept, and the 200
    showed the caller a phantom. More than two decimal places is now a 422
    at the boundary, so the value judged IS the value stored."""
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    put = f"/api/payroll/employees/{emp_id}/deposit-accounts"
    for bad in ("99.999", "0.004"):
        r = c.put(put, json={"accounts": [
            _account(op, emp_id, 1, allocation_type="percent",
                     allocation_value=bad),
            _account(op, emp_id, 2),
        ]}, headers=hdr)
        assert r.status_code == 422, (bad, r.text)
    # And the overflow that used to 500 off the vault door (numeric field
    # overflow past Numeric(10,2)'s 8 integer digits) is a 422 too.
    r = c.put(put, json={"accounts": [
        _account(op, emp_id, 1, allocation_type="amount",
                 allocation_value="999999999.00"),
        _account(op, emp_id, 2),
    ]}, headers=hdr)
    assert r.status_code == 422
    assert db_session.execute(select(DepositAccount)).scalars().all() == []


def test_clearing_a_chain_is_an_explicit_act(db_engine, db_session, tmp_path):
    """An empty accounts list ERASES every deposit account. A buggy client
    one retry from an empty array must not de-bank an employee with a 200 —
    clear=true is the confirmation, and the erase is audited like any other
    write."""
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    put = f"/api/payroll/employees/{emp_id}/deposit-accounts"
    assert c.put(put, json={"accounts": [_account(op, emp_id, 1)]},
                 headers=hdr).status_code == 200

    r = c.put(put, json={"accounts": []}, headers=hdr)
    assert r.status_code == 422
    db_session.expire_all()
    assert len(db_session.execute(select(DepositAccount)).scalars().all()) == 1, (
        "a refused clear must leave the chain untouched"
    )

    r = c.put(put, json={"accounts": [], "clear": True}, headers=hdr)
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.execute(select(DepositAccount)).scalars().all() == []


def test_gm_cannot_touch_deposit_accounts(db_engine, db_session, tmp_path):
    """A GM can onboard and terminate; deposit ROUTING is payroll-tier, same
    as every other vault surface."""
    op = SoftwareOpener.generate(key_id="dev-1")
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier, op)
    emp_id = _seed(db_session)
    gm = {"Authorization": f"Bearer {mint(roles=['property_gm'], sub='gm', scopes=[{'property_id': 'HISJ', 'department_id': None}])}"}
    assert c.get(f"/api/payroll/employees/{emp_id}/deposit-accounts",
                 headers=gm).status_code == 403
    assert c.put(f"/api/payroll/employees/{emp_id}/deposit-accounts",
                 json={"accounts": []}, headers=gm).status_code == 403


def test_the_profile_surface_no_longer_speaks_bank(db_engine, db_session, tmp_path):
    """The old fields leave ProfileBody and ProfileStatus in the SAME release
    (no dual write paths through money routing): a client still sending
    bank_account gets it ignored — nothing stored — and status no longer
    advertises the retired booleans."""
    op, c, hdr, emp_id = _setup(db_engine, db_session, tmp_path)
    r = c.put(
        f"/api/payroll/employees/{emp_id}/profile",
        json={
            "ssn": seal_for_test(op.public_key(), b"123-45-6789",
                                 aad=f"{emp_id}:ssn".encode()).to_json(),
            "bank_account": seal_for_test(
                op.public_key(), b"000123456",
                aad=f"{emp_id}:bank_account".encode()).to_json(),
        },
        headers=hdr,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ssn_on_file"] is True
    assert "bank_account_on_file" not in body
    assert "account_type" not in body
    # The retired write path is DEAD, not merely undocumented: the column is
    # gone (e5b0dropbank), so nothing can have stored the ignored field --
    # and no deposit row appeared either.
    assert db_session.execute(select(DepositAccount)).scalars().all() == []
