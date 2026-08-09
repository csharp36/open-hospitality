"""MONEY-lens probes: is the punch REALLY never blocked?

Each test punches as an enrolled employee whose stored template row is
damaged in a way the real world produces (key rotation, truncated blob,
dimension drift, degenerate vector). The wage rule says every one of these
must record a punch (201). A 500 is a blocked punch.
"""

import io

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests.authkit import make_authkit
from tests.employees import make_employee
from usali.db import make_session_factory
from usali.face_match import embedding_to_bytes
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.kiosk import mint_device_token
from usali.models import EmployeeFaceTemplate, KioskDevice, Organization, Property, Punch
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_JPEG = b"\xff\xd8\xff\xe0 fake-frame"
_V = "fake-embedder-v1"
_MARIA = [1.0, 0.0, 0.0, 0.0]


class FakeEngine:
    model_version = _V

    def embed_largest_face(self, image_bytes: bytes) -> list[float] | None:
        return list(_MARIA)


def _app(db_engine, tmp_path):
    return create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
        face_engine_factory=lambda: FakeEngine(),
    )


def _seed(db_session, embedding: bytes, raw_sql_blob: bytes | None = None):
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA", wage_jurisdiction="US-CA"))
    db_session.flush()
    token, token_hash = mint_device_token()
    device = KioskDevice(property_id="HISJ", name="iPad",
                         token_hash=token_hash, enrolled_by="adm")
    maria = make_employee(db_session, property_id="HISJ",
                          full_name="Maria Lopez", pay_type="hourly")
    db_session.add(device)
    db_session.flush()
    db_session.add(EmployeeFaceTemplate(
        employee_id=maria.employee_id, embedding=embedding,
        model_version=_V, created_by="adm"))
    db_session.commit()
    if raw_sql_blob is not None:
        # Bypass EncryptedBytes: simulates a blob the CURRENT key cannot
        # decrypt (i.e. a rotated field_encryption_key).
        db_session.execute(
            text("UPDATE employee_face_template SET embedding = :b"),
            {"b": raw_sql_blob},
        )
        db_session.commit()
    return token, maria.employee_id


def _punch(c, token, employee_id):
    return c.post(
        "/api/kiosk/punch",
        data={"employee_id": employee_id, "punch_type": "clock_in"},
        files={"photo": ("p.jpg", io.BytesIO(_JPEG), "image/jpeg")},
        headers={"X-Kiosk-Token": token},
    )


def _assert_punch_records(db_engine, db_session, tmp_path, token, emp_id):
    c = TestClient(_app(db_engine, tmp_path), raise_server_exceptions=False)
    r = _punch(c, token, emp_id)
    assert r.status_code == 201, f"PUNCH BLOCKED: {r.status_code} {r.text}"
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.employee_id == emp_id


def test_dimension_mismatched_template_must_not_block_the_punch(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Template stored with 3 floats, probe has 4 (same model_version
    string). zip(strict=True) in cosine_similarity raises ValueError."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    token, emp = _seed(db_session, embedding_to_bytes([1.0, 0.0, 0.0]))
    _assert_punch_records(db_engine, db_session, tmp_path, token, emp)


def test_truncated_template_blob_must_not_block_the_punch(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Blob length not a multiple of 4: struct.unpack raises struct.error
    inside embedding_from_bytes."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    token, emp = _seed(db_session, b"\x01\x02\x03\x04\x05")
    _assert_punch_records(db_engine, db_session, tmp_path, token, emp)


def test_zero_vector_template_must_not_block_the_punch(
    db_engine, db_session, tmp_path, monkeypatch
):
    """All-zero template: cosine_similarity divides by zero."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    token, emp = _seed(db_session, embedding_to_bytes([0.0, 0.0, 0.0, 0.0]))
    _assert_punch_records(db_engine, db_session, tmp_path, token, emp)


def test_undecryptable_template_must_not_block_the_punch(
    db_engine, db_session, tmp_path, monkeypatch
):
    """field_encryption_key rotated: AES-GCM decrypt of the stored template
    raises InvalidTag when the row is LOADED — before the try block."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    token, emp = _seed(
        db_session, embedding_to_bytes(_MARIA),
        raw_sql_blob=b"\x00" * 40,  # not a valid nonce||ct||tag under any key
    )
    _assert_punch_records(db_engine, db_session, tmp_path, token, emp)


def test_models_missing_at_factory_must_not_block_the_punch(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The engine FACTORY raises (model files absent — FaceModelsMissing at
    construction, not at embed). Punch must still record with NULL fields."""
    from usali.face_match import FaceModelsMissing

    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")
    token, emp = _seed(db_session, embedding_to_bytes(_MARIA))

    def boom():
        raise FaceModelsMissing("models not fetched")

    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=InMemoryPhotoStore(),
        face_engine_factory=boom,
    )
    c = TestClient(app, raise_server_exceptions=False)
    r = _punch(c, token, emp)
    assert r.status_code == 201, f"PUNCH BLOCKED: {r.status_code} {r.text}"
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state is None and p.match_score is None
