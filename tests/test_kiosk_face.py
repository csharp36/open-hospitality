"""F4: kiosk identification, search fallback, and server-side punch verify.

The load-bearing postures (the Pillar F face-match design
decisions 2, 3, 8):

- THE PUNCH IS NEVER BLOCKED by matching (wage rule). Engine down, stale
  template, no face, unencoded state — every failure records the punch and
  degrades the match fields, never the other way around.
- THE CLIENT NEVER SETS MATCH FIELDS. The server re-verifies the punch's
  own photo against the CLAIMED employee's template (1:1) — a kiosk client
  claiming "verified" in the form is ignored. Identify is the UX selector;
  the punch derives the truth itself.
- Identify is property-confined (the device's population only), writes NO
  audit rows and stores nothing — the score on the punch is the record.
- Search is the no-roster fallback: 3+ chars, active + this property,
  capped, wildcards treated literally.
"""

import io

from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.authkit import make_authkit
from tests.employees import make_employee
from usali.db import make_session_factory
from usali.face_match import embedding_to_bytes
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.kiosk import mint_device_token
from usali.models import (
    AuditEvent,
    EmployeeFaceTemplate,
    KioskDevice,
    Organization,
    Property,
    Punch,
)
from usali.photo_store import InMemoryPhotoStore
from usali.server import create_app
from usali.mapping.property_registry import DEFAULT_ORG_ALIAS

_JPEG = b"\xff\xd8\xff\xe0 fake-frame"
_V = "fake-embedder-v1"

# Orthogonal unit vectors: cosine 1.0 with self, 0.0 across.
_MARIA = [1.0, 0.0, 0.0, 0.0]
_BOB = [0.0, 1.0, 0.0, 0.0]
_CARLA = [0.0, 0.0, 1.0, 0.0]


class FakeEngine:
    model_version = _V

    def __init__(self) -> None:
        self.next_result: list[float] | None = list(_MARIA)
        self.raise_on_embed: Exception | None = None

    def embed_largest_face(self, image_bytes: bytes) -> list[float] | None:
        if self.raise_on_embed is not None:
            raise self.raise_on_embed
        return self.next_result


def _app(db_engine, tmp_path, store=None, engine=None):
    return create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=make_authkit()[0],
        keycloak_admin=InMemoryKeycloakAdmin(),
        photo_store=store or InMemoryPhotoStore(),
        face_engine_factory=lambda: engine or FakeEngine(),
    )


def _seed(db_session, *, hisj_jurisdiction="US-CA"):
    """Two CA properties (unless overridden), one HISJ kiosk, three staff:
    Maria (HISJ, enrolled), Bob (HISJ, enrolled), Sam (SSSJ, enrolled with
    MARIA'S vector — the cross-property impostor for confinement tests)."""
    db_session.add(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id="HISJ", org_id=1, name="HISJ",
                            pms_source="OPERA",
                            wage_jurisdiction=hisj_jurisdiction))
    db_session.add(Property(property_id="SSSJ", org_id=1, name="SSSJ",
                            pms_source="AUTOCLERK", wage_jurisdiction="US-CA"))
    db_session.flush()
    token, token_hash = mint_device_token()
    device = KioskDevice(property_id="HISJ", name="iPad",
                         token_hash=token_hash, enrolled_by="adm")
    maria = make_employee(db_session, property_id="HISJ",
                          full_name="Maria Lopez", pay_type="hourly")
    bob = make_employee(db_session, property_id="HISJ",
                        full_name="Bob Marsh", pay_type="hourly")
    sam = make_employee(db_session, property_id="SSSJ",
                        full_name="Sam Otherhotel", pay_type="hourly")
    db_session.add_all([device, maria, bob, sam])
    db_session.flush()
    for emp, vec in ((maria, _MARIA), (bob, _BOB), (sam, _MARIA)):
        db_session.add(EmployeeFaceTemplate(
            employee_id=emp.employee_id, embedding=embedding_to_bytes(vec),
            model_version=_V, created_by="adm"))
    db_session.commit()
    return token, maria.employee_id, bob.employee_id, sam.employee_id


def _enable(monkeypatch):
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "true")


def _punch(c, token, employee_id, data=None, **extra_form):
    return c.post(
        "/api/kiosk/punch",
        data={"employee_id": employee_id, "punch_type": "clock_in", **extra_form},
        files={"photo": ("p.jpg", io.BytesIO(data or _JPEG), "image/jpeg")},
        headers={"X-Kiosk-Token": token},
    )


def _identify(c, token, data=None):
    return c.post(
        "/api/kiosk/identify",
        files={"photo": ("p.jpg", io.BytesIO(data or _JPEG), "image/jpeg")},
        headers={"X-Kiosk-Token": token},
    )


# --- punch: server-side 1:1 verification --------------------------------------


def test_matching_photo_records_verified_with_its_score(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = list(_MARIA)
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "verified"
    assert p.match_score is not None and p.match_score >= 0.99


def test_someone_elses_face_records_unverified_with_the_low_score(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Buddy punching: Bob taps Maria's name, camera sees Bob. The punch
    RECORDS (hours worked are hours worked) — flagged for the GM."""
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = list(_BOB)  # the face at the camera is Bob's
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "unverified"
    assert p.match_score is not None and p.match_score < 0.1


def test_unenrolled_employee_records_no_template(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, _, _, _ = _seed(db_session)
    newbie = make_employee(db_session, property_id="HISJ",
                           full_name="Newton New", pay_type="hourly")
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path))
    assert _punch(c, token, newbie.employee_id).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "no_template"
    assert p.match_score is None


def test_disabled_flag_leaves_match_fields_null(
    db_engine, db_session, tmp_path, monkeypatch
):
    """NULL means 'recorded without matching' — deliberately distinct from
    no_template. The dark flag must produce exactly the pre-F punch."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "false")
    token, maria, _, _ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state is None and p.match_score is None


def test_unencoded_jurisdiction_records_the_punch_without_matching(
    db_engine, db_session, tmp_path, monkeypatch
):
    """A Nevada kiosk with the flag on: biometric processing may not run
    there (no encoded posture), but the PUNCH records — wage law does not
    care about our posture table."""
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session, hisj_jurisdiction="US-NV")
    c = TestClient(_app(db_engine, tmp_path))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state is None and p.match_score is None


def test_engine_failure_never_blocks_the_punch(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The wage rule's hard case: matching infrastructure down. The punch
    records with NULL fields ('recorded without matching' — exactly true)."""
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    engine = FakeEngine()
    engine.raise_on_embed = RuntimeError("onnxruntime exploded")
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state is None and p.match_score is None


def test_a_stale_model_template_degrades_to_unverified(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Template from an older embedder: not comparable, never scored. The
    punch records unverified (grey — a human looks), score NULL."""
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    db_session.execute(
        EmployeeFaceTemplate.__table__.update()
        .where(EmployeeFaceTemplate.employee_id == maria)
        .values(model_version="ancient-v0")
    )
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "unverified" and p.match_score is None


def test_no_face_in_the_frame_records_unverified(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = None
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "unverified" and p.match_score is None


def test_a_negative_cosine_clamps_instead_of_violating_the_check(
    db_engine, db_session, tmp_path, monkeypatch
):
    """cosine can be negative; match_score's CHECK is [0,1]. Without the
    clamp this punch 500s at the INSERT — a blocked punch, the exact wage
    failure — on the most alien face possible."""
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = [-x for x in _MARIA]  # cosine exactly -1.0
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "unverified" and p.match_score == 0.0


def test_the_client_cannot_claim_verified(
    db_engine, db_session, tmp_path, monkeypatch
):
    """A form field match_state=verified from the device is IGNORED — the
    server derives the state from the photo it was handed."""
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = list(_BOB)  # camera sees the wrong face
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    r = _punch(c, token, maria, match_state="verified", match_score="1.0")
    assert r.status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "unverified"


# --- identify -----------------------------------------------------------------


def test_identify_returns_the_matched_candidate(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = _identify(c, token)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "matched"
    assert body["employee_id"] == maria
    assert body["full_name"] == "Maria Lopez"
    # The raw similarity never leaves the server (F8 disclosure lens): a
    # device token holder could hill-climb a synthetic probe toward a real
    # template if every call reported how close it got. The confirm-tap UI
    # needs only state + identity; the score on the eventual PUNCH is the
    # record, visible only to approvers.
    assert "score" not in body


def test_identify_is_property_confined(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Sam at the OTHER hotel is enrolled with the same vector the camera
    sees. The HISJ device must resolve to HISJ's Maria — and Sam's
    existence must be invisible here. With Maria removed, the same face
    finds NOBODY (no_match is Sam staying invisible; his 1.0-similarity
    template is not in this device's population)."""
    _enable(monkeypatch)
    token, maria, _, sam = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    body = _identify(c, token).json()
    assert body["employee_id"] == maria

    template = db_session.execute(select(EmployeeFaceTemplate).where(
        EmployeeFaceTemplate.employee_id == maria)).scalar_one()
    db_session.delete(template)
    db_session.commit()
    body = _identify(c, token).json()
    assert body["state"] == "no_match"
    assert "employee_id" not in body or body["employee_id"] is None


def test_identify_reports_no_face_but_collapses_no_template(
    db_engine, db_session, tmp_path, monkeypatch
):
    """no_face is a fact about the SUBMITTED photo — safe to report, the
    kiosk can prompt a retry. no_template is a fact about the DATABASE
    (nobody at this property is enrolled): collapsed into no_match so the
    device population cannot read enrollment posture (F8 disclosure lens,
    same line E3 drew with the byte-identical kiosk 403). The UI routes
    both to search anyway."""
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = None
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    assert _identify(c, token).json()["state"] == "no_face"

    db_session.execute(EmployeeFaceTemplate.__table__.delete())
    db_session.commit()
    c2 = TestClient(_app(db_engine, tmp_path))
    body = _identify(c2, token).json()
    assert body["state"] == "no_match"
    assert "score" not in body


def test_identify_never_returns_a_score_on_a_miss(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The no_match nearest-similarity is the sharpest oracle of all: with
    no identity attached it still tells a probe how close it got to
    SOMEONE. Nothing resembling a score may appear on any miss."""
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = [0.7, 0.7141428, 0.0, 0.0]  # close to nobody
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    body = _identify(c, token).json()
    assert body["state"] == "no_match"
    assert "score" not in body


def test_identify_with_a_corrupt_template_degrades_to_search_not_500(
    db_engine, db_session, tmp_path, monkeypatch
):
    """One damaged template row (undecryptable after a key rotation,
    truncated blob) must not take the whole camera flow down with a 500 —
    the kiosk gets the same handled 503 an engine outage gets, and the
    frontend falls back to search (wage rule)."""
    from sqlalchemy import text

    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    db_session.execute(
        text("UPDATE employee_face_template SET embedding = :b"),
        {"b": b"\x00" * 40},  # bypasses EncryptedBytes: undecryptable
    )
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path), raise_server_exceptions=False)
    r = _identify(c, token)
    assert r.status_code == 503, f"identify 500ed: {r.status_code} {r.text}"


def test_identify_refuses_when_matching_is_dark(
    db_engine, db_session, tmp_path, monkeypatch
):
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "false")
    token, *_ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    assert _identify(c, token).status_code == 409


def test_identify_writes_no_audit_and_stores_nothing(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Never match events (plan F3 note): the probe photo is discarded, no
    audit row, no punch — the score on a real punch is the only record."""
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    store = InMemoryPhotoStore()
    c = TestClient(_app(db_engine, tmp_path, store=store))
    assert _identify(c, token).status_code == 200
    assert db_session.execute(select(AuditEvent)).first() is None
    assert store.keys() == set()
    assert db_session.execute(select(Punch)).first() is None


def test_identify_excludes_inactive_staff(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    from usali.models import Employee

    db_session.execute(Employee.__table__.update().where(
        Employee.employee_id == maria).values(employment_status="leave"))
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path))
    body = _identify(c, token).json()
    assert body["state"] != "matched" or body["employee_id"] != maria


# --- search -------------------------------------------------------------------


def _search(c, token, q):
    return c.get("/api/kiosk/search", params={"q": q},
                 headers={"X-Kiosk-Token": token})


def test_search_requires_three_characters(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    assert _search(c, token, "ma").status_code == 422
    assert _search(c, token, "  m  ").status_code == 422


def test_search_finds_by_substring_within_the_property_only(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    rows = _search(c, token, "mar").json()
    # Maria Lopez and Bob Marsh both contain "mar" (case-insensitive);
    # Sam at the other hotel never appears.
    names = {r["full_name"] for r in rows}
    assert names == {"Maria Lopez", "Bob Marsh"}
    assert all(set(r) == {"employee_id", "full_name"} for r in rows)


def test_search_treats_wildcards_literally(
    db_engine, db_session, tmp_path, monkeypatch
):
    """'%%%' must not become 'match everyone' — the fallback search must
    never be a roster enumeration."""
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    assert _search(c, token, "%%%").json() == []
    assert _search(c, token, "___").json() == []


def test_search_is_capped(db_engine, db_session, tmp_path, monkeypatch):
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    for i in range(9):
        make_employee(db_session, property_id="HISJ",
                      full_name=f"Marlon Clone{i}", pay_type="hourly")
    db_session.commit()
    c = TestClient(_app(db_engine, tmp_path))
    assert len(_search(c, token, "marlon").json()) == 5


def test_search_works_with_matching_dark(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Search is the punch fallback, not biometric processing — it must
    work when matching is off (and F5's old-flow kiosk may use it)."""
    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "false")
    token, *_ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    assert _search(c, token, "maria").status_code == 200


# --- config -------------------------------------------------------------------


def test_config_reports_matching_reality_per_device(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The kiosk frontend chooses camera-first vs roster flow from this:
    true only when the flag is on AND the device's state has an encoded
    posture."""
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    r = c.get("/api/kiosk/config", headers={"X-Kiosk-Token": token})
    assert r.status_code == 200
    assert r.json() == {"matching_enabled": True}

    monkeypatch.setenv("USALI_BIOMETRIC_MATCHING_ENABLED", "false")
    assert c.get("/api/kiosk/config", headers={"X-Kiosk-Token": token}
                 ).json() == {"matching_enabled": False}


def test_config_is_false_in_an_unencoded_jurisdiction(
    db_engine, db_session, tmp_path, monkeypatch
):
    _enable(monkeypatch)
    token, *_ = _seed(db_session, hisj_jurisdiction="US-NV")
    c = TestClient(_app(db_engine, tmp_path))
    assert c.get("/api/kiosk/config", headers={"X-Kiosk-Token": token}
                 ).json() == {"matching_enabled": False}


def test_one_stale_model_template_does_not_poison_identification(
    db_engine, db_session, tmp_path, monkeypatch
):
    """The identify population EXCLUDES templates from another model
    version. Without that filter, ONE employee re-enrolled under an old
    model makes identify() raise ModelVersionMismatch — a 500 for every
    camera identification at the property until the row is fixed (the F8
    migration-lens S2 mutant survived exactly because no fixture mixed
    model versions in one population)."""
    _enable(monkeypatch)
    token, maria, _, _ = _seed(db_session)
    oscar = make_employee(db_session, property_id="HISJ",
                          full_name="Old Model Oscar", pay_type="hourly")
    db_session.flush()
    db_session.add(EmployeeFaceTemplate(
        employee_id=oscar.employee_id,
        embedding=embedding_to_bytes(list(_CARLA)),
        model_version="stale-model-v0", created_by="adm"))
    db_session.commit()

    c = TestClient(_app(db_engine, tmp_path), raise_server_exceptions=False)
    r = _identify(c, token)
    assert r.status_code == 200, f"identify 500ed: {r.status_code} {r.text}"
    body = r.json()
    assert body["state"] == "matched"
    assert body["employee_id"] == maria


def test_a_punch_score_exactly_at_threshold_is_verified(
    db_engine, db_session, tmp_path, monkeypatch
):
    """`score >= threshold` on the punch row too — identical unit vectors
    give a float-exact 1.0 against a threshold of 1.0, so the green/red
    boundary is pinned with no rounding slack (F8 S7 epsilon mutant)."""
    _enable(monkeypatch)
    monkeypatch.setenv("USALI_FACE_MATCH_THRESHOLD", "1.0")
    token, maria, _, _ = _seed(db_session)
    engine = FakeEngine()
    engine.next_result = list(_MARIA)
    c = TestClient(_app(db_engine, tmp_path, engine=engine))
    assert _punch(c, token, maria).status_code == 201
    p = db_session.execute(select(Punch)).scalar_one()
    assert p.match_state == "verified"
    assert p.match_score == 1.0


def test_identify_refuses_a_non_jpeg_photo(
    db_engine, db_session, tmp_path, monkeypatch
):
    """Same guards as the punch endpoint, pinned on identify too (F8)."""
    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    assert _identify(c, token, data=b"PNG not jpeg").status_code == 422


def test_identify_refuses_an_oversized_photo(
    db_engine, db_session, tmp_path, monkeypatch
):
    from usali.kiosk import _MAX_PHOTO_BYTES

    _enable(monkeypatch)
    token, *_ = _seed(db_session)
    c = TestClient(_app(db_engine, tmp_path))
    huge = _JPEG + b"\x00" * _MAX_PHOTO_BYTES
    assert _identify(c, token, data=huge).status_code == 413
