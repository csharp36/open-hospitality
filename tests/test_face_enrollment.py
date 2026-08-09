"""F3: enrollment — the one writer of face templates.

The postures under test, from the Pillar F face-match design
decisions 5-7 and 9:

- Onboarder roles only (org_admin | property_gm), GM scope checked against
  the employee's placements — the PATCH-employee posture.
- The dark flag gates ENROLLMENT too: computing a template while matching
  is disabled would be biometric processing the flag claims isn't
  happening.
- The jurisdiction gate refuses any placement property whose consent
  regime is unencoded (or unset) — an IL hire cannot be enrolled under
  CA's notice flow.
- Enrollment records WHICH notice version the employee was shown; replace
  is the only update shape; termination deletes template AND reference
  photo; every write is audited by resource id (never image bytes, and
  never match events — the score on the punch is the record).
"""

import io
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee, place
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.face_match import embedding_to_bytes
from usali.models import (
    AuditEvent,
    Department,
    EmployeeFaceTemplate,
    Organization,
    Property,
)
from usali.face_enrollment import write_face_template
from usali.onboarding import terminate_employee
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.photo_store import InMemoryPhotoStore
from usali.tenancy import bind_org_context
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-payload" * 10
_VEC = [0.25, -0.5, 0.75, 1.0]


class FakeEngine:
    """Deterministic stand-in for OnnxFaceEngine: no models, no numpy."""

    model_version = "fake-embedder-v1"

    def __init__(self) -> None:
        self.next_result: list[float] | None = list(_VEC)

    def embed_largest_face(self, image_bytes: bytes) -> list[float] | None:
        return self.next_result


def _world(db_session, *, extra_property_jurisdiction=None):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    if extra_property_jurisdiction is not None:
        db_session.add(Property(property_id="RENO", org_id=1, name="RENO",
                                pms_source="OPERA",
                                wage_jurisdiction=extra_property_jurisdiction))
    db_session.flush()
    dept = Department(property_id="HISJ", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    emp = make_employee(db_session, property_id="HISJ",
                        department_id=dept.department_id,
                        full_name="Faye Templeton", pay_type="hourly")
    db_session.commit()
    # L4: role authority is DB grants per SUBJECT — each minted persona
    # gets its own sub + the matching grant (an org_admin grant on the
    # gm/pa subs would silently widen the very scope these tests pin).
    grant_role(db_session, "org_admin")  # the default-sub org_admin
    grant_role(db_session, "org_admin", sub="adm-1")
    grant_role(db_session, "payroll_admin", sub="pa")
    grant_role(db_session, "accountant", sub="acct")
    for gm_sub in ("gm-1", "gm-2"):
        grant_role(db_session, "property_gm", sub=gm_sub, property_id="HISJ")
    return emp


def _client(db_engine, tmp_path, verifier, engine=None, store=None):
    app = create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier,
        photo_store=store or InMemoryPhotoStore(),
        face_engine_factory=lambda: engine or FakeEngine(),
    )
    return TestClient(app)


def _enroll(c, tok, employee_id, data=_JPEG):
    return c.post(
        f"/api/employees/{employee_id}/face-template",
        files={"photo": ("face.jpg", io.BytesIO(data), "image/jpeg")},
        headers={"Authorization": f"Bearer {tok}"},
    )


def _enable(monkeypatch, notice=""):
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    monkeypatch.setenv("USALI_BIOMETRIC_NOTICE_VERSION", notice)


def test_scoped_gm_enrolls_with_notice_recorded(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch, notice="2026-07-notice-v1")
    emp = _world(db_session)
    verifier, mint = make_authkit()
    store = InMemoryPhotoStore()
    c = _client(db_engine, tmp_path, verifier, store=store)
    tok = mint(roles=["property_gm"], sub="gm-1",
               scopes=[{"property_id": "HISJ", "department_id": None}])

    r = _enroll(c, tok, emp.employee_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["model_version"] == "fake-embedder-v1"
    assert body["notice_version"] == "2026-07-notice-v1"

    row = db_session.execute(select(EmployeeFaceTemplate)).scalar_one()
    assert row.employee_id == emp.employee_id
    assert row.embedding == embedding_to_bytes(_VEC)
    assert row.model_version == "fake-embedder-v1"
    assert row.notice_version == "2026-07-notice-v1"
    assert row.notified_on == date.today()
    assert row.created_by == "gm-1"
    # The reference photo is stored (encrypted by the store) under the
    # deterministic per-employee key, now org-namespaced (L5): the active
    # org (org 1 here) prefixes the key — termination rederives the same one.
    assert store.keys() == {f"org/1/face-reference/{emp.employee_id}.jpg"}

    audit = db_session.execute(select(AuditEvent).where(
        AuditEvent.action == "face_template_enrolled")).scalar_one()
    assert audit.actor_subject == "gm-1"
    assert audit.resource_type == "employee"
    assert audit.resource_id == str(emp.employee_id)


def test_dev_enrollment_without_a_notice_version_records_the_null_pair(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch, notice="")
    emp = _world(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"])
    assert _enroll(c, tok, emp.employee_id).status_code == 201
    row = db_session.execute(select(EmployeeFaceTemplate)).scalar_one()
    assert row.notice_version is None and row.notified_on is None


def test_reenrollment_replaces_the_single_row_and_audits_replace(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    emp = _world(db_session)
    verifier, mint = make_authkit()
    engine = FakeEngine()
    c = _client(db_engine, tmp_path, verifier, engine=engine)
    tok = mint(roles=["org_admin"], sub="adm-1")

    assert _enroll(c, tok, emp.employee_id).status_code == 201
    engine.next_result = [9.0, 9.0, 9.0, 9.0]
    assert _enroll(c, tok, emp.employee_id).status_code == 201

    rows = db_session.execute(select(EmployeeFaceTemplate)).scalars().all()
    assert len(rows) == 1
    assert rows[0].embedding == embedding_to_bytes([9.0, 9.0, 9.0, 9.0])
    actions = [a for (a,) in db_session.execute(select(AuditEvent.action))]
    assert actions.count("face_template_enrolled") == 1
    assert actions.count("face_template_replaced") == 1


def test_the_dark_flag_gates_enrollment_too(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Computing a template while matching is disabled would be biometric
    processing the flag says isn't happening — refused, nothing stored."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "false")
    emp = _world(db_session)
    verifier, mint = make_authkit()
    store = InMemoryPhotoStore()
    c = _client(db_engine, tmp_path, verifier, store=store)
    tok = mint(roles=["org_admin"])
    r = _enroll(c, tok, emp.employee_id)
    assert r.status_code == 409
    assert "disabled" in r.json()["detail"]
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None
    assert store.keys() == set()


def test_an_unencoded_jurisdiction_refuses_enrollment(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The employee also works at a Nevada property: NV's consent regime is
    unencoded, so enrolling this person is refused outright — the posture
    table's whole purpose (plan decision 6)."""
    _enable(monkeypatch)
    emp = _world(db_session, extra_property_jurisdiction="US-NV")
    place(db_session, emp, property_id="RENO", department_id=None,
          position_id=None, is_primary=False)
    db_session.commit()
    verifier, mint = make_authkit()
    store = InMemoryPhotoStore()
    c = _client(db_engine, tmp_path, verifier, store=store)
    tok = mint(roles=["org_admin"])
    r = _enroll(c, tok, emp.employee_id)
    assert r.status_code == 422
    assert "US-NV" in r.json()["detail"]
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None
    assert store.keys() == set()


def test_a_property_with_no_jurisdiction_refuses_enrollment(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    emp = _world(db_session, extra_property_jurisdiction=None)
    db_session.execute(
        Property.__table__.update().where(
            Property.property_id == "HISJ").values(wage_jurisdiction=None)
    )
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"])
    r = _enroll(c, tok, emp.employee_id)
    assert r.status_code == 422
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None


def test_no_face_in_the_photo_is_a_422_and_stores_nothing(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    emp = _world(db_session)
    verifier, mint = make_authkit()
    engine = FakeEngine()
    engine.next_result = None
    store = InMemoryPhotoStore()
    c = _client(db_engine, tmp_path, verifier, engine=engine, store=store)
    tok = mint(roles=["org_admin"])
    r = _enroll(c, tok, emp.employee_id)
    assert r.status_code == 422
    assert "no face" in r.json()["detail"]
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None
    assert store.keys() == set()


def test_non_jpeg_is_refused(db_engine, db_session, tmp_path, monkeypatch):
    _enable(monkeypatch)
    emp = _world(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"])
    r = _enroll(c, tok, emp.employee_id, data=b"\x89PNG\r\n" + b"x" * 40)
    assert r.status_code == 422
    assert "JPEG" in r.json()["detail"]


def test_out_of_scope_gm_and_non_onboarder_roles_are_403(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    emp = _world(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    other_gm = mint(roles=["property_gm"], sub="gm-2",
                    scopes=[{"property_id": "SSSJ", "department_id": None}])
    assert _enroll(c, other_gm, emp.employee_id).status_code == 403
    for role, sub in (("payroll_admin", "pa"), ("accountant", "acct")):
        assert _enroll(
            c, mint(roles=[role], sub=sub), emp.employee_id
        ).status_code == 403
        assert c.get(f"/api/employees/{emp.employee_id}/face-template",
                     headers={"Authorization": f"Bearer {mint(roles=[role], sub=sub)}"},
                     ).status_code == 403


def test_gm_with_scope_over_only_one_of_two_placements_is_403(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The E3 review's all->any class, pinned where it can actually bite: a
    dual-property worker and a GM holding ONE of the two properties. A
    template enrolled here would identify the person at the OTHER hotel's
    kiosk too — whole-person scope means every placement, and only a
    fixture like this one distinguishes all() from any()."""
    _enable(monkeypatch)
    emp = _world(db_session)
    place(db_session, emp, property_id="SSSJ", department_id=None,
          position_id=None, is_primary=False)
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    hisj_gm = mint(roles=["property_gm"], sub="gm-1",
                   scopes=[{"property_id": "HISJ", "department_id": None}])
    assert _enroll(c, hisj_gm, emp.employee_id).status_code == 403
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None
    both_gm = mint(roles=["property_gm"], sub="gm-2",
                   scopes=[{"property_id": "HISJ", "department_id": None},
                           {"property_id": "SSSJ", "department_id": None}])
    assert _enroll(c, both_gm, emp.employee_id).status_code == 201


def test_terminated_employee_cannot_be_enrolled(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    emp = _world(db_session)
    terminate_employee(db_session, InMemoryKeycloakAdmin(), emp.employee_id,
                       actor_subject="adm", on_date=date.today())
    db_session.commit()
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"])
    assert _enroll(c, tok, emp.employee_id).status_code == 409


def test_unknown_employee_is_404(db_engine, db_session, tmp_path, monkeypatch):
    _enable(monkeypatch)
    _world(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"])
    assert _enroll(c, tok, 4242).status_code == 404


def test_status_reports_enrollment_without_ever_returning_the_embedding(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch, notice="2026-07-notice-v1")
    emp = _world(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    tok = mint(roles=["org_admin"])
    url = f"/api/employees/{emp.employee_id}/face-template"
    headers = {"Authorization": f"Bearer {tok}"}

    r = c.get(url, headers=headers)
    assert r.status_code == 200 and r.json()["enrolled"] is False

    assert _enroll(c, tok, emp.employee_id).status_code == 201
    r = c.get(url, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["enrolled"] is True
    assert body["model_version"] == "fake-embedder-v1"
    assert body["notice_version"] == "2026-07-notice-v1"
    # The vector must never cross the wire, under any key.
    assert "embedding" not in body
    for value in body.values():
        assert not (isinstance(value, str) and len(value) > 100)


def test_delete_removes_template_and_reference_photo_and_audits(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    emp = _world(db_session)
    verifier, mint = make_authkit()
    store = InMemoryPhotoStore()
    c = _client(db_engine, tmp_path, verifier, store=store)
    tok = mint(roles=["org_admin"], sub="adm-1")
    assert _enroll(c, tok, emp.employee_id).status_code == 201
    assert store.keys() != set()

    r = c.delete(f"/api/employees/{emp.employee_id}/face-template",
                 headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 204
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None
    assert store.keys() == set()
    audit = db_session.execute(select(AuditEvent).where(
        AuditEvent.action == "face_template_deleted")).scalar_one()
    assert audit.resource_id == str(emp.employee_id)


def test_termination_deletes_template_and_reference_photo(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The retention posture's hard edge: separation deletes the biometric.
    Exercised through the API route so the wiring (photo store handed to
    terminate_employee) is what's proven, not a helper called by hand."""
    _enable(monkeypatch)
    emp = _world(db_session)
    verifier, mint = make_authkit()
    store = InMemoryPhotoStore()
    c = _client(db_engine, tmp_path, verifier, store=store)
    adm = mint(roles=["org_admin"], sub="adm-1")
    assert _enroll(c, adm, emp.employee_id).status_code == 201

    r = c.post(f"/api/employees/{emp.employee_id}/terminate",
               headers={"Authorization": f"Bearer {adm}"})
    assert r.status_code == 200, r.text
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None
    assert store.keys() == set()
    audit = db_session.execute(select(AuditEvent).where(
        AuditEvent.action == "face_template_deleted")).scalar_one()
    assert audit.resource_id == str(emp.employee_id)


def test_write_face_template_enrolls_then_replaces_and_reports_the_action(
    db_session,
):
    """F7: the extracted writer the API route and the demo seed share — one
    code path owns replace semantics, the notice pair, and the audit action."""
    emp = _world(db_session)
    # The writer reads the active org off the session (L5) to prefix the
    # reference-photo key; bind org 1, exactly as the seed/API session is.
    bind_org_context(db_session, 1)
    engine = FakeEngine()
    store = InMemoryPhotoStore()

    action = write_face_template(
        db_session, engine, store, employee_id=emp.employee_id,
        photo_bytes=_JPEG, actor_subject="demo-seed", notice_version=None,
    )
    assert action == "face_template_enrolled"
    db_session.commit()
    row = db_session.execute(select(EmployeeFaceTemplate)).scalar_one()
    assert row.embedding == embedding_to_bytes(_VEC)
    assert row.model_version == "fake-embedder-v1"
    assert row.created_by == "demo-seed"
    assert row.notice_version is None and row.notified_on is None
    assert store.keys() == {f"org/1/face-reference/{emp.employee_id}.jpg"}

    engine.next_result = [1.0, 0.0, 0.0, 0.0]
    action = write_face_template(
        db_session, engine, store, employee_id=emp.employee_id,
        photo_bytes=_JPEG, actor_subject="demo-seed",
        notice_version="2026-07-notice-v1",
    )
    assert action == "face_template_replaced"
    db_session.commit()
    row = db_session.execute(select(EmployeeFaceTemplate)).scalar_one()
    assert row.embedding == embedding_to_bytes([1.0, 0.0, 0.0, 0.0])
    assert row.notice_version == "2026-07-notice-v1"
    assert row.notified_on == date.today()
    actions = [a for (a,) in db_session.execute(select(AuditEvent.action))]
    assert actions.count("face_template_enrolled") == 1
    assert actions.count("face_template_replaced") == 1


def test_write_face_template_with_no_face_writes_nothing(db_session):
    emp = _world(db_session)
    engine = FakeEngine()
    engine.next_result = None
    store = InMemoryPhotoStore()
    assert write_face_template(
        db_session, engine, store, employee_id=emp.employee_id,
        photo_bytes=_JPEG, actor_subject="demo-seed", notice_version=None,
    ) is None
    db_session.commit()
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None
    assert store.keys() == set()
    face_audits = db_session.execute(select(AuditEvent).where(
        AuditEvent.action.like("face_template_%"))).scalars().all()
    assert face_audits == []


def test_terminating_an_unenrolled_employee_writes_no_deletion_audit(
    db_engine, db_session, tmp_path, monkeypatch
):
    """An audit row claiming a template was deleted when none existed would
    be a false record — the trail must say what happened, exactly."""
    _enable(monkeypatch)
    emp = _world(db_session)
    verifier, mint = make_authkit()
    c = _client(db_engine, tmp_path, verifier)
    adm = mint(roles=["org_admin"])
    r = c.post(f"/api/employees/{emp.employee_id}/terminate",
               headers={"Authorization": f"Bearer {adm}"})
    assert r.status_code == 200
    deletions = db_session.execute(select(AuditEvent).where(
        AuditEvent.action == "face_template_deleted")).scalars().all()
    assert deletions == []
