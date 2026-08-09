"""F1: what the face-template migration leaves behind, exercised not read.

The CHECKs are the contract the approval gate (F6) leans on: a punch can only
claim `verified` with a real score behind it, a score can't float free of a
state, and a template can't record a notice version without the date it was
shown (or vice versa) — the notice pair is the CA compliance artifact
(the Pillar F face-match design decisions 3, 7, 8).

NULL match_state means "recorded before/without matching" — every punch the
system has ever written so far. It is deliberately distinct from
`no_template` (matching ran, found nobody enrolled).
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from tests.employees import make_employee, place
from usali.models import (
    Department,
    EmployeeFaceTemplate,
    KioskDevice,
    Organization,
    Position,
    Property,
    Punch,
)
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_DAY = date(2026, 7, 7)


def _world(db_session):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="El Sendero LLC"))
    db_session.add(Property(property_id="SJCES", org_id=1, name="HIE",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    dept = Department(property_id="SJCES", name="Front Office")
    db_session.add(dept)
    db_session.flush()
    pos = Position(department_id=dept.department_id, title="FRONT DESK",
                   flsa_exempt=False)
    device = KioskDevice(property_id="SJCES", name="iPad", token_hash="a" * 64,
                         enrolled_by="adm")
    db_session.add_all([pos, device])
    db_session.flush()
    emp = make_employee(db_session, place_primary=False, property_id="SJCES",
                        full_name="Faye Templeton", pay_type="hourly")
    db_session.flush()
    place(db_session, emp, property_id="SJCES",
          department_id=dept.department_id, position_id=pos.position_id,
          is_primary=True)
    db_session.flush()
    return emp, device


def _punch(emp, device, **match):
    return Punch(
        employee_id=emp.employee_id, kiosk_device_id=device.device_id,
        punch_type="clock_in",
        punched_at=datetime(2026, 7, 7, 9, tzinfo=UTC),
        business_date=_DAY, **match,
    )


# --- punch.match_state / match_score -----------------------------------------


def test_a_legacy_punch_carries_null_match_fields(db_session):
    """Every pre-F punch and every matching-disabled punch: both columns
    NULL, insert unchanged. The kiosk flow must never be blocked by the
    matching feature's schema (wage rule)."""
    emp, device = _world(db_session)
    p = _punch(emp, device)
    db_session.add(p)
    db_session.commit()
    assert p.match_state is None and p.match_score is None


def test_verified_requires_a_score(db_session):
    emp, device = _world(db_session)
    db_session.add(_punch(emp, device, match_state="verified"))
    with pytest.raises(IntegrityError, match="ck_punch_verified_has_score"):
        db_session.commit()


def test_a_score_requires_a_state(db_session):
    emp, device = _world(db_session)
    db_session.add(_punch(emp, device, match_score=0.97))
    with pytest.raises(IntegrityError, match="ck_punch_score_has_state"):
        db_session.commit()


def test_match_state_vocabulary_is_closed(db_session):
    emp, device = _world(db_session)
    db_session.add(_punch(emp, device, match_state="probably", match_score=0.5))
    with pytest.raises(IntegrityError, match="ck_punch_match_state"):
        db_session.commit()


def test_match_score_is_bounded_to_the_unit_interval(db_session):
    emp, device = _world(db_session)
    db_session.add(_punch(emp, device, match_state="verified", match_score=1.2))
    with pytest.raises(IntegrityError, match="ck_punch_score_range"):
        db_session.commit()


def test_the_three_lawful_states_insert(db_session):
    emp, device = _world(db_session)
    for state, score in (("verified", 0.91), ("unverified", 0.42),
                         ("unverified", None), ("no_template", None)):
        db_session.add(_punch(emp, device, match_state=state, match_score=score))
    db_session.commit()


# --- employee_face_template ---------------------------------------------------


def test_one_template_per_employee(db_session):
    """Re-enrollment REPLACES (plan decision 5) — the unique constraint is
    what makes replace the only possible write shape."""
    emp, _ = _world(db_session)
    db_session.add(EmployeeFaceTemplate(
        employee_id=emp.employee_id, embedding=b"\x01" * 512,
        model_version="arcface-r50-v1", created_by="adm"))
    db_session.commit()
    db_session.add(EmployeeFaceTemplate(
        employee_id=emp.employee_id, embedding=b"\x02" * 512,
        model_version="arcface-r50-v1", created_by="adm"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_notice_version_and_date_are_a_pair(db_session):
    """A version without a date (or a date without a version) is a
    half-recorded compliance fact — refused at the schema."""
    emp, _ = _world(db_session)
    db_session.add(EmployeeFaceTemplate(
        employee_id=emp.employee_id, embedding=b"\x01" * 512,
        model_version="arcface-r50-v1", created_by="adm",
        notice_version="2026-07-notice-v1"))
    with pytest.raises(IntegrityError, match="ck_face_template_notice_pair"):
        db_session.commit()


def test_the_embedding_is_encrypted_at_rest(db_session):
    """A breach of unencrypted biometric data is the one CA
    private-right-of-action scenario (§1798.150) — so the raw column must
    never contain the plaintext vector, and the ORM must round-trip it."""
    emp, _ = _world(db_session)
    plaintext = bytes(range(256)) * 2
    db_session.add(EmployeeFaceTemplate(
        employee_id=emp.employee_id, embedding=plaintext,
        model_version="arcface-r50-v1", created_by="adm",
        notice_version="2026-07-notice-v1", notified_on=_DAY))
    db_session.commit()
    db_session.expire_all()

    raw = db_session.execute(text(
        "SELECT embedding FROM employee_face_template WHERE employee_id = :e"
    ), {"e": emp.employee_id}).scalar_one()
    assert bytes(raw) != plaintext
    assert plaintext not in bytes(raw)

    row = db_session.execute(select(EmployeeFaceTemplate).where(
        EmployeeFaceTemplate.employee_id == emp.employee_id)).scalar_one()
    assert row.embedding == plaintext
    assert row.notice_version == "2026-07-notice-v1"


def test_deleting_an_employee_template_is_possible_and_final(db_session):
    """Termination DELETES the row (retention posture: separation + <=30
    days). Nothing may FK to the template — deletion must never cascade
    into punches or timecards."""
    emp, _ = _world(db_session)
    db_session.add(EmployeeFaceTemplate(
        employee_id=emp.employee_id, embedding=b"\x01" * 512,
        model_version="arcface-r50-v1", created_by="adm"))
    db_session.commit()
    row = db_session.execute(select(EmployeeFaceTemplate)).scalar_one()
    db_session.delete(row)
    db_session.commit()
    assert db_session.execute(select(EmployeeFaceTemplate)).first() is None


def test_the_template_table_shape(db_session):
    columns = {c["name"] for c in
               inspect(db_session.bind).get_columns("employee_face_template")}
    assert {
        "template_id", "employee_id", "embedding", "model_version",
        "notice_version", "notified_on", "created_by", "created_at",
    } <= columns


def test_the_template_not_nulls_hold(db_session):
    """Raw-SQL INSERTs (the ORM would refuse earlier — ETL and hand-run SQL
    would not): each required template column must be NOT NULL in the
    DATABASE, not just promised by the model. The F8 review's surviving
    mutant S1: relaxing `embedding` to nullable in the migration kept the
    whole suite green — the recorded E3 'same revision, two schemas'
    class."""
    emp, _ = _world(db_session)
    db_session.commit()
    base = {
        "employee_id": emp.employee_id, "embedding": b"\x01",
        "model_version": "arcface-r50-v1", "created_by": "adm",
    }
    for column in ("employee_id", "embedding", "model_version", "created_by"):
        row = {**base, column: None}
        with pytest.raises(IntegrityError, match="not-null|NotNullViolation"):
            db_session.execute(text(
                "INSERT INTO employee_face_template "
                "(employee_id, embedding, model_version, created_by) "
                "VALUES (:employee_id, :embedding, :model_version, "
                ":created_by)"
            ), row)
        db_session.rollback()
