from fastapi.testclient import TestClient

from usali.gusto_mock import create_mock_gusto

_H = {"Authorization": "Bearer mock"}


def _client():
    return TestClient(create_mock_gusto())


def _create_employee(c, **overrides):
    body = {
        "full_name": "Hank H", "ssn": "123-45-6789",
        "deposit_accounts": [{
            "bank_routing": "021000021", "bank_account": "000123456",
            "account_type": "checking", "allocation_type": "remainder",
            "allocation_value": None,
        }],
    }
    body.update(overrides)
    return c.post("/v1/companies/mock/employees", headers=_H, json=body)


def test_requires_bearer_token():
    c = _client()
    assert c.post("/v1/companies/mock/employees", json={}).status_code == 401


def test_every_route_requires_bearer():
    c = _client()
    assert c.post("/v1/companies/mock/payrolls", json={}).status_code == 401
    assert c.get("/v1/companies/mock/payrolls/x").status_code == 401


def test_wrong_token_is_401():
    c = _client()
    bad = {"Authorization": "Bearer wrong"}
    assert c.post("/v1/companies/mock/employees", headers=bad, json={}).status_code == 401


def test_employee_then_payroll_round_trip():
    c = _client()
    r = c.post("/v1/companies/mock/employees", headers=_H, json={
        "full_name": "Hank H", "ssn": "123-45-6789",
        "deposit_accounts": [{
            "bank_routing": "021000021", "bank_account": "000123456",
            "account_type": "checking", "allocation_type": "remainder",
            "allocation_value": None,
        }],
    })
    assert r.status_code == 201
    uuid = r.json()["uuid"]

    sub = c.post("/v1/companies/mock/payrolls", headers=_H, json={
        "period_start": "2026-07-06", "period_end": "2026-07-19",
        "check_date": "2026-07-24",
        "entries": [{"employee_uuid": uuid, "regular_hours": "16.00",
                     "overtime_hours": "0.00", "double_overtime_hours": "0.00",
                     "hourly_rate": "20.00", "sick_hours": "0.00"}],
    })
    assert sub.status_code == 201
    payroll_uuid = sub.json()["payroll_uuid"]

    got = c.get(f"/v1/companies/mock/payrolls/{payroll_uuid}", headers=_H)
    assert got.status_code == 200
    body = got.json()
    assert body["status"] == "processed"
    comp = body["employee_compensations"][0]
    assert comp["gross_pay"] == "320.00"       # dollars as decimal STRINGS
    assert comp["employee_taxes"] == "48.00"   # 15%
    assert comp["employer_taxes"] == "32.00"   # 10%
    assert comp["net_pay"] == "272.00"


def test_overtime_and_double_time_are_priced():
    c = _client()
    uuid = _create_employee(c).json()["uuid"]
    sub = c.post("/v1/companies/mock/payrolls", headers=_H, json={
        "period_start": "2026-07-06", "period_end": "2026-07-19",
        "check_date": "2026-07-24",
        "entries": [{"employee_uuid": uuid, "regular_hours": "8.00",
                     "overtime_hours": "2.00", "double_overtime_hours": "1.00",
                     "hourly_rate": "20.00", "sick_hours": "0.00"}],
    })
    got = c.get(f"/v1/companies/mock/payrolls/{sub.json()['payroll_uuid']}", headers=_H)
    # 8*20 + 2*20*1.5 + 1*20*2 = 160 + 60 + 40 = 260
    assert got.json()["employee_compensations"][0]["gross_pay"] == "260.00"


def test_missing_employee_field_is_422_and_never_echoes_pii():
    c = _client()
    r = _create_employee(c, full_name="")
    assert r.status_code == 422
    text = r.text
    assert "full_name" in text
    assert "123-45-6789" not in text    # the SSN must never appear in a response
    assert "000123456" not in text


def test_unknown_employee_in_payroll_is_422():
    c = _client()
    r = c.post("/v1/companies/mock/payrolls", headers=_H, json={
        "period_start": "2026-07-06", "period_end": "2026-07-19",
        "check_date": "2026-07-24",
        "entries": [{"employee_uuid": "ghost", "regular_hours": "8.00",
                     "overtime_hours": "0.00", "double_overtime_hours": "0.00",
                     "hourly_rate": "20.00"}],
    })
    assert r.status_code == 422


def test_unknown_payroll_is_404():
    c = _client()
    assert c.get("/v1/companies/mock/payrolls/nope", headers=_H).status_code == 404


def test_state_is_per_instance():
    a, b = _client(), _client()
    uuid = _create_employee(a).json()["uuid"]
    # An employee created on one instance does not exist on another.
    r = b.post("/v1/companies/mock/payrolls", headers=_H, json={
        "period_start": "2026-07-06", "period_end": "2026-07-19",
        "check_date": "2026-07-24",
        "entries": [{"employee_uuid": uuid, "regular_hours": "8.00",
                     "overtime_hours": "0.00", "double_overtime_hours": "0.00",
                     "hourly_rate": "20.00"}],
    })
    assert r.status_code == 422


def test_an_entry_without_sick_hours_is_refused_by_name():
    """G4 made sick_hours part of the wire contract; a payload without it
    is a caller that would silently drop paid leave — 422, named field."""
    c = _client()
    uuid = _create_employee(c).json()["uuid"]
    r = c.post("/v1/companies/mock/payrolls", headers=_H, json={
        "period_start": "2026-07-06", "period_end": "2026-07-19",
        "check_date": "2026-07-24",
        "entries": [{"employee_uuid": uuid, "regular_hours": "8.00",
                     "overtime_hours": "0.00", "double_overtime_hours": "0.00",
                     "hourly_rate": "20.00"}],
    })
    assert r.status_code == 422
    assert "sick_hours" in r.json()["detail"]
