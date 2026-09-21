"""The gate's own pin (design section 5): nothing under the ingest directories
carries the uploaded bytes after processing, on success or failure."""

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.test_night_audit import _admin_headers, _client
from usali.ingestion import ProcessingError, process_document_bytes, process_file
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import NightAuditState, Organization
from usali.server import create_app

SAMPLES = Path("docs/reference/samples")
PACK = SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf"
FLASH = SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf"
TRIAL_BALANCE = SAMPLES / "Trial Balance 07.07.2026 - Opera.pdf"

_VERIFIER, _MINT = make_authkit()


def _seed(db_session, *dicts):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    for d in dicts:
        load_mappings(db_session, f"mapping/{d}.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()


def _assert_no_file_carries(root: Path, data: bytes) -> None:
    sha = hashlib.sha256(data).hexdigest()
    probe = data[len(data) // 2: len(data) // 2 + 16]
    for f in root.rglob("*"):
        if f.is_file():
            blob = f.read_bytes()
            assert hashlib.sha256(blob).hexdigest() != sha, f
            assert probe not in blob, f


def test_success_files_a_redacted_artifact_and_no_raw_bytes(db_session, tmp_path):
    _seed(db_session, "skytouch")
    data = PACK.read_bytes()
    results = process_document_bytes(
        db_session, data, PACK.name, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
    )
    assert len(results) == 2
    art = json.loads(results[0].destination.read_text())
    assert art["sha256"] == hashlib.sha256(data).hexdigest()
    assert {s["report_type"] for s in art["sections"]} == {"hotel_journal", "hotel_statistics"}
    assert set(art["sections_dropped"]) == {"A/R Aging", "Cancellation List"}
    assert len({r.destination for r in results}) == 1
    _assert_no_file_carries(tmp_path, data)


def test_failure_files_an_error_record_and_no_raw_bytes(db_session, tmp_path, founding_org):
    # No properties seeded: detection cannot resolve the property.
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    db_session.commit()
    data = FLASH.read_bytes()
    with pytest.raises(ProcessingError):
        process_document_bytes(
            db_session, data, FLASH.name, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail",
        )
    records = list((tmp_path / "fail").glob("*.error.json"))
    assert len(records) == 1 and "words" not in json.loads(records[0].read_text())
    assert not (tmp_path / "done").exists() or not any((tmp_path / "done").iterdir())
    _assert_no_file_carries(tmp_path, data)


def test_process_file_leaves_the_callers_file_in_place(db_session, tmp_path):
    _seed(db_session, "opera")
    src = tmp_path / "in" / FLASH.name
    src.parent.mkdir()
    shutil.copy(FLASH, src)
    r = process_file(db_session, src, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail")
    assert src.exists()  # the caller owns its file; nothing moves it
    assert r.destination.name.endswith(".redacted.json")
    # The only copy of the flash under the ingest dirs is the source the caller placed.
    _assert_no_file_carries(tmp_path / "done", FLASH.read_bytes())


def _night_audit_world(db_session, *dicts):
    """The world an operator upload needs: org 1 under the alias the minted
    token carries, plus the schedules/mappings/properties `_seed` loads."""
    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.commit()
    _seed(db_session, *dicts)


def test_ingest_endpoint_leaves_no_raw_bytes_anywhere(db_session, founding_org, tmp_path):
    """The API boundary, not just the library: an accepted upload leaves the
    redacted artifact and nothing else under the ingest directories."""
    _seed(db_session, "opera")
    client = TestClient(create_app(inbox_dir=tmp_path / "inbox",
                                   processed_dir=tmp_path / "processed",
                                   failed_dir=tmp_path / "failed",
                                   token_verifier=_VERIFIER))
    client.headers["Authorization"] = f"Bearer {_MINT(roles=['accountant'])}"
    payload = FLASH.read_bytes()

    resp = client.post("/ingest", files={"file": (FLASH.name, payload, "application/pdf")})

    assert resp.status_code == 200, resp.text
    _assert_no_file_carries(tmp_path, payload)
    assert list((tmp_path / "processed").glob("*.redacted.json"))
    assert not (tmp_path / "inbox").exists() or not any((tmp_path / "inbox").iterdir())


def test_night_audit_single_upload_leaves_no_raw_bytes_anywhere(
    db_session, db_engine, tmp_path
):
    _night_audit_world(db_session, "opera")
    db_session.add(NightAuditState(property_id="HISJ",
                                   current_business_date=date(2026, 7, 7)))
    db_session.commit()
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)
    payload = TRIAL_BALANCE.read_bytes()

    r = client.post(
        "/api/properties/HISJ/night-audit/upload", headers=headers,
        files={"file": (TRIAL_BALANCE.name, payload, "application/pdf")},
    )

    assert r.status_code == 201, r.text
    _assert_no_file_carries(tmp_path, payload)
    assert not (tmp_path / "inbox").exists() or not any((tmp_path / "inbox").iterdir())


def test_night_audit_pack_upload_leaves_no_raw_bytes_anywhere(
    db_session, db_engine, tmp_path
):
    _night_audit_world(db_session, "skytouch")
    db_session.add(NightAuditState(property_id="STDEMO",
                                   current_business_date=date(2026, 6, 21)))
    db_session.commit()
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)
    payload = PACK.read_bytes()

    r = client.post(
        "/api/properties/STDEMO/night-audit/upload", headers=headers,
        files={"file": (PACK.name, payload, "application/pdf")},
    )

    assert r.status_code == 201, r.text
    _assert_no_file_carries(tmp_path, payload)
    assert not (tmp_path / "inbox").exists() or not any((tmp_path / "inbox").iterdir())
