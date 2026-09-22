"""OH-23 Task 3: the emailed night-audit webhook, POST /api/intake/email.

Every case here posts a signed, synthetic RFC822 message the way the
Cloudflare worker does (D-OH23.1: five `X-Intake-*` headers, the raw message
as the body) and asserts on three surfaces at once — the HTTP response, the
rows the request left in the database, and what the ingest directories hold.
The third is `tests/test_ingestion_boundary._assert_no_file_carries`, the
gate's own inventory: this route is a new way for a report to reach
`process_document_bytes`, so it inherits the gate and every case ends by
proving the raw message and the attachment are on disk nowhere.
"""

import hashlib
import io
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests.authkit import make_authkit
from tests.intake_helpers import SENDER, _message, _post
from tests.orgwall import app_role_url
from tests.test_ingestion_boundary import _assert_no_file_carries
from usali import intake, intake_api
from usali.config import get_settings
from usali.db import make_engine, make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import create_first_property, seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import (
    EmailIntakeEvent,
    IngestBatch,
    IngestionCoverage,
    PropertyIntakeAddress,
)
from usali.night_audit_api import _MAX_UPLOAD_BYTES
from usali.ratelimit import RateLimiter
from usali.server import create_app
from usali.tenancy import bind_org_context

SAMPLES = Path("docs/reference/samples")
FLASH = SAMPLES / "Manager Flash 07.07.2026 - Opera.pdf"
PACK = SAMPLES / "SkyTouch - Standard Audit Pack (mock).pdf"
HOTELKEY_PDF = SAMPLES / "HotelKey - Hotel Statistics (mock).pdf"
HOTELKEY_FIXTURES = Path("tests/fixtures/hotelkey")

_SETTINGS = get_settings()
_DOMAIN = _SETTINGS.email_intake_domain

# One keypair for the whole module: create_app wants a token verifier, and
# nothing here carries an operator token — the webhook is not an OIDC surface.
_VERIFIER, _ = make_authkit()

# Every address this module mints. Lowercase, `[a-z0-9-]`, as
# ck_property_intake_address_local_part_lower and intake.local_part require.
HISJ_ADDRESS = "na-hisj"
SSSJ_ADDRESS = "na-sssj"
STDEMO_ADDRESS = "na-stdemo"
HKDEMO_ADDRESS = "na-hkdemo"


# --- the world ---------------------------------------------------------------


def _seed(db_session, *dicts):
    """Schedules, the named mapping dictionaries, and the four registry
    properties (HISJ, SSSJ, STDEMO, HKDEMO) under org 1."""
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    for d in dicts:
        load_mappings(db_session, f"mapping/{d}.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()


def _address(db_session, local_part, property_id, *, org_id=1, revoked_at=None):
    row = PropertyIntakeAddress(
        local_part=local_part, org_id=org_id, property_id=property_id,
        revoked_at=revoked_at,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _app(engine, tmp_path):
    return create_app(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(engine),
        token_verifier=_VERIFIER,
        keycloak_admin=InMemoryKeycloakAdmin(),
    )


@pytest.fixture
def client(db_engine, tmp_path):
    return TestClient(_app(db_engine, tmp_path))


def _clean(tmp_path, *payloads):
    """The gate's inventory over the app's own directories, for the raw
    message and for every attachment it carried."""
    for payload in payloads:
        _assert_no_file_carries(
            payload,
            processed=tmp_path / "processed",
            failed=tmp_path / "failed",
            inbox=tmp_path / "inbox",
        )


def _events(session):
    return list(session.scalars(select(EmailIntakeEvent).order_by(
        EmailIntakeEvent.event_id)))


def _batches(session, status):
    return list(session.scalars(select(IngestBatch).where(
        IngestBatch.status == status).order_by(IngestBatch.batch_id)))


def _error_records(tmp_path):
    return sorted((tmp_path / "failed").glob("*.error.json"))


# --- the closed outcome set ---------------------------------------------------


def test_the_literal_outcomes_match_the_event_tables_closed_set():
    """`intake_api.IntakeOutcome` is the same ten names as
    `intake.INTAKE_OUTCOMES`, which `tests/test_intake_schema.py::
    test_intake_outcomes_match_the_check_constraint` pins to the CHECK. The
    Literal is what makes mypy --strict refuse a stray outcome string at the
    call sites in intake_api; this is what keeps it from drifting."""
    assert intake_api.INTAKE_OUTCOME_NAMES == intake.INTAKE_OUTCOMES
    assert "unknown_address" not in intake.INTAKE_OUTCOMES
    assert intake_api.UNKNOWN_ADDRESS == "unknown_address"


def test_an_attachment_is_capped_at_the_upload_endpoints_limit():
    """The per-attachment cap is the upload endpoint's own constant, not a
    second number that can drift from it."""
    assert intake_api._MAX_ATTACHMENT_BYTES == _MAX_UPLOAD_BYTES
    assert intake_api._MAX_ATTACHMENTS == 10


# --- authentication (D-OH23.2) ------------------------------------------------


def test_a_bad_signature_is_401_and_writes_nothing(db_session, client, tmp_path):
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}", secret="not-the-secret")

    assert r.status_code == 401, r.text
    # No detail: which check failed is itself a signal to an unauthenticated
    # caller, so a bad signature and a stale timestamp answer identically.
    assert r.json() == {"detail": "unauthorized"}
    assert _events(db_session) == []
    assert db_session.scalar(select(func.count()).select_from(IngestBatch)) == 0
    _clean(tmp_path, raw, payload)


def test_a_stale_timestamp_is_401_and_writes_nothing(db_session, client, tmp_path):
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])
    stale = datetime.now(UTC) - timedelta(
        seconds=_SETTINGS.email_intake_window_seconds + 60)

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}",
              timestamp=str(int(stale.timestamp())))

    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "unauthorized"}
    assert _events(db_session) == []
    assert db_session.scalar(select(func.count()).select_from(IngestBatch)) == 0
    _clean(tmp_path, raw, payload)


def test_an_oversize_message_is_refused_before_anything_is_read(
    db_session, db_engine, tmp_path, monkeypatch
):
    """413, not 401: a non-2xx is what makes the worker forward the message to
    the fallback mailbox (D-OH23.9) instead of dropping it."""
    monkeypatch.setenv("USALI_EMAIL_INTAKE_MAX_BYTES", "2000")
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    client = TestClient(_app(db_engine, tmp_path))
    raw = _message(attachments=[("big.pdf", b"%PDF-1.4" + b"x" * 4000)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 413, r.text
    assert _events(db_session) == []
    _clean(tmp_path, raw)


def test_the_rate_limiter_refuses_the_flood(db_session, db_engine, tmp_path):
    """One worker posts here, so the limiter is keyed on a CONSTANT: it is a
    ceiling on the route, not a per-client budget. Two callers at different
    addresses spend the same twelve-a-minute budget, which is what stops a
    flood buying itself more room by varying its source address — the
    per-IP/global pair `/api/preview` needs does not apply to a route with
    one legitimate caller.
    """
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    app = _app(db_engine, tmp_path)
    assert isinstance(app.state.intake_rate_limiter, RateLimiter)
    app.state.intake_rate_limiter = RateLimiter(max_events=2, window_seconds=60.0)
    one = TestClient(app, client=("10.0.0.1", 40000))
    two = TestClient(app, client=("198.51.100.7", 40000))
    raw = _message()
    to = f"{HISJ_ADDRESS}@{_DOMAIN}"

    codes = [_post(c, raw, to=to).status_code for c in (one, two, one, two)]

    assert codes == [200, 200, 429, 429]
    _clean(tmp_path, raw)


# --- addressing (D-OH23.3) ----------------------------------------------------


def test_an_unknown_local_part_is_answered_but_never_recorded(
    db_session, client, tmp_path
):
    _seed(db_session, "opera")
    raw = _message()

    r = _post(client, raw, to=f"na-nobody@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unknown_address"
    assert _events(db_session) == []
    _clean(tmp_path, raw)


def test_a_recipient_at_another_domain_is_an_unknown_address(
    db_session, client, tmp_path
):
    """`intake.local_part` ignores the domain, so the domain is checked here:
    a message to the right local part at somebody else's domain resolves no
    address at all."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    raw = _message()

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@not-our-intake-domain.test")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unknown_address"
    assert _events(db_session) == []
    _clean(tmp_path, raw)


def test_a_revoked_address_is_recorded_under_its_own_org(
    db_session, client, tmp_path
):
    _seed(db_session, "opera")
    row = _address(db_session, HISJ_ADDRESS, "HISJ",
                   revoked_at=datetime.now(UTC) - timedelta(days=1))
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "revoked_address"
    event = _events(db_session)[0]
    assert (event.outcome, event.org_id, event.address_id) == (
        "revoked_address", 1, row.address_id)
    assert event.attachments == []
    assert _batches(db_session, "transformed") == []
    _clean(tmp_path, raw, payload)


# --- sender policy (D-OH23.4) -------------------------------------------------


def test_an_unauthenticated_sender_is_rejected(db_session, client, tmp_path):
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}", auth="none")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "sender_rejected"
    assert [e.outcome for e in _events(db_session)] == ["sender_rejected"]
    assert _batches(db_session, "transformed") == []
    _clean(tmp_path, raw, payload)


def test_a_pass_for_nobody_in_particular_is_rejected(db_session, client, tmp_path):
    """A bare `dkim=pass` carries no `header.d`, so it vouches for no domain
    and cannot be aligned with the envelope sender (D-OH23.4)."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}", auth="dkim=pass")

    assert r.json()["outcome"] == "sender_rejected"
    assert _batches(db_session, "transformed") == []
    _clean(tmp_path, raw, payload)


def test_a_bounce_with_no_envelope_sender_is_rejected(db_session, client, tmp_path):
    """A null return path is what a bounce carries; it aligns with nothing,
    so it is recorded and not opened."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}", frm="",
              auth="dkim=pass header.d=pms.test")

    assert r.json()["outcome"] == "sender_rejected"
    event = _events(db_session)[0]
    assert event.envelope_from == ""
    assert _batches(db_session, "transformed") == []
    _clean(tmp_path, raw, payload)


# --- attachments and outcomes -------------------------------------------------


def test_a_message_with_no_attachment_is_recorded(db_session, client, tmp_path):
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    raw = _message()

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json() == {"outcome": "no_attachment", "attachments": []}
    assert [e.outcome for e in _events(db_session)] == ["no_attachment"]
    _clean(tmp_path, raw)


def test_the_opera_flash_ingests_for_its_property(db_session, client, tmp_path):
    _seed(db_session, "opera")
    row = _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "ingested"
    assert len(body["attachments"]) == 1
    entry = body["attachments"][0]
    assert set(entry) == {"name", "sha256", "bytes", "outcome", "batch_id"}
    assert entry["outcome"] == "ingested"
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entry["bytes"] == len(payload)
    assert entry["name"] == "flash.pdf"

    batches = _batches(db_session, "transformed")
    assert len(batches) == 1
    assert entry["batch_id"] == batches[0].batch_id

    event = _events(db_session)[0]
    assert (event.outcome, event.property_id, event.address_id, event.org_id) == (
        "ingested", "HISJ", row.address_id, 1)
    assert event.attachments == body["attachments"]
    assert event.message_id == "<one@pms.test>"
    assert event.envelope_from == SENDER

    coverage = list(db_session.scalars(select(IngestionCoverage)))
    assert len(coverage) == 1
    assert coverage[0].report_type == "manager_flash"
    _clean(tmp_path, raw, payload)


def test_the_same_message_again_is_a_duplicate(db_session, client, tmp_path):
    """A PMS scheduler re-sends (D-OH23.6). The second message's attachment
    already has a `transformed` batch on its sha256, so it is not processed
    and the batch count does not move."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])
    to = f"{HISJ_ADDRESS}@{_DOMAIN}"

    assert _post(client, raw, to=to).json()["outcome"] == "ingested"
    before = db_session.scalar(select(func.count()).select_from(IngestBatch))

    r = _post(client, raw, to=to)

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "duplicate"
    assert r.json()["attachments"][0]["outcome"] == "duplicate"
    assert db_session.scalar(select(func.count()).select_from(IngestBatch)) == before
    assert [e.outcome for e in _events(db_session)] == ["ingested", "duplicate"]
    _clean(tmp_path, raw, payload)


def test_a_pack_ingests_every_recognized_section(db_session, client, tmp_path):
    _seed(db_session, "skytouch")
    _address(db_session, STDEMO_ADDRESS, "STDEMO")
    payload = PACK.read_bytes()
    raw = _message(attachments=[("pack.pdf", payload)])

    r = _post(client, raw, to=f"{STDEMO_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "ingested"
    batches = _batches(db_session, "transformed")
    assert {b.report_type for b in batches} == {"hotel_journal", "hotel_statistics"}
    assert {b.file_hash for b in batches} == {hashlib.sha256(payload).hexdigest()}
    # One attachment, one entry: a pack stages a batch per recognized section,
    # and `batch_id` names the first of them (D-OH23.7 gives the entry one
    # batch_id; the sha256 beside it is what finds the rest).
    entry = r.json()["attachments"][0]
    assert entry["batch_id"] == min(b.batch_id for b in batches)
    _clean(tmp_path, raw, payload)


def test_a_report_for_another_property_is_refused(db_session, client, tmp_path):
    """The Opera flash detects as HISJ; sent to SSSJ's address it stages
    nothing (D-OH23.5)."""
    _seed(db_session, "opera")
    _address(db_session, SSSJ_ADDRESS, "SSSJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{SSSJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "wrong_property"
    assert r.json()["attachments"][0]["outcome"] == "wrong_property"
    assert db_session.scalar(select(func.count()).select_from(IngestBatch)) == 0
    assert [e.outcome for e in _events(db_session)] == ["wrong_property"]
    _clean(tmp_path, raw, payload)


def test_a_hotelkey_night_arrives_as_one_message(db_session, client, tmp_path):
    """HotelKey exports four files a night; an auditor attaches all four to
    one mail."""
    _seed(db_session, "hotelkey")
    _address(db_session, HKDEMO_ADDRESS, "HKDEMO")
    files = [
        ("Hotel Statistics.pdf", HOTELKEY_PDF.read_bytes()),
        ("Settlement By Payment Type.xlsx",
         (HOTELKEY_FIXTURES / "Settlement By Payment Type.xlsx").read_bytes()),
        ("All Payments.xlsx",
         (HOTELKEY_FIXTURES / "All Payments.xlsx").read_bytes()),
        ("AR Invoice Aging.xlsx",
         (HOTELKEY_FIXTURES / "AR Invoice Aging.xlsx").read_bytes()),
    ]
    raw = _message(attachments=files)

    r = _post(client, raw, to=f"{HKDEMO_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "ingested"
    assert [a["outcome"] for a in body["attachments"]] == ["ingested"] * 4
    batches = _batches(db_session, "transformed")
    assert {b.report_type for b in batches} == {
        "hotel_statistics", "settlement", "all_payments", "ar_aging"}
    _clean(tmp_path, raw, *[data for _name, data in files])


# --- failures (D-OH23.9) ------------------------------------------------------


def test_a_corrupt_pdf_is_failed_with_a_batch_and_an_error_record(
    db_session, client, tmp_path
):
    """The gate records a processing failure; the event is written AFTER it,
    in its own transaction, so the failed batch (already committed by the
    gate's own rollback-and-record) and the event both survive."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = b"%PDF-1.4 this is not a real pdf at all"
    raw = _message(attachments=[("broken.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "failed"
    entry = r.json()["attachments"][0]
    assert entry["outcome"] == "failed"
    assert entry["error"]
    assert len(_batches(db_session, "failed")) == 1
    assert _batches(db_session, "transformed") == []
    assert len(_error_records(tmp_path)) == 1
    event = _events(db_session)[0]
    assert event.outcome == "failed"
    assert event.attachments[0]["outcome"] == "failed"
    _clean(tmp_path, raw, payload)


def test_a_zip_that_is_not_a_workbook_is_failed(db_session, client, tmp_path):
    """`is_xlsx` is the `PK\\x03\\x04` magic, so any zip is admitted as an
    attachment and fails inside the workbook reader."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("hello.txt", "this is not a spreadsheet")
    payload = buf.getvalue()
    raw = _message(attachments=[("report.xlsx", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json()["attachments"][0]["outcome"] == "failed"
    assert len(_batches(db_session, "failed")) == 1
    assert _batches(db_session, "transformed") == []
    assert len(_error_records(tmp_path)) == 1
    _clean(tmp_path, raw, payload)


def test_an_oversize_attachment_is_never_processed(
    db_session, client, tmp_path, monkeypatch
):
    monkeypatch.setattr(intake_api, "_MAX_ATTACHMENT_BYTES", 128)
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    entry = r.json()["attachments"][0]
    assert entry["outcome"] == "failed"
    assert "too large" in entry["error"]
    assert db_session.scalar(select(func.count()).select_from(IngestBatch)) == 0
    assert _error_records(tmp_path) == []
    _clean(tmp_path, raw, payload)


def test_attachments_past_the_cap_are_recorded_and_not_processed(
    db_session, client, tmp_path, monkeypatch
):
    monkeypatch.setattr(intake_api, "_MAX_ATTACHMENTS", 1)
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    first = b"%PDF-1.4 the only one that is opened"
    second = b"%PDF-1.4 the one past the cap"
    raw = _message(attachments=[("one.pdf", first), ("two.pdf", second)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    entries = r.json()["attachments"]
    assert [e["outcome"] for e in entries] == ["failed", "failed"]
    assert entries[1]["error"] == "too_many_attachments"
    # Only the first was opened: one failed batch, one error record.
    assert len(_batches(db_session, "failed")) == 1
    assert len(_error_records(tmp_path)) == 1
    _clean(tmp_path, raw, first, second)


# --- what the event may carry (D-OH23.7) --------------------------------------


def test_event_text_carries_no_card_numbers(db_session, client, tmp_path):
    """Every piece of author-chosen text the event keeps goes through
    `redaction.mask_pans` first: the subject, and each attachment's name.

    The attachment here also FAILS, so the third string an event can carry —
    an error — is exercised too, and the name is followed past the event into
    the `source_file` the batch records and the stem of the filed error
    record, which is where an unmasked filename would otherwise land.
    """
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = b"%PDF-1.4 this is not a real pdf at all"
    raw = _message(subject="Report 4111 1111 1111 1111",
                   message_id="<4111111111111111@pms.test>",
                   attachments=[("4111111111111111.pdf", payload)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}",
              frm="4111111111111111@pms.test",
              auth="dkim=pass header.d=pms.test (envelope 4111 1111 1111 1111)")

    assert r.status_code == 200, r.text
    event = _events(db_session)[0]
    assert event.subject is not None
    assert "4111" not in event.subject
    assert "•••• 1111" in event.subject
    # Every string column, not just the subject: a receiver interpolates the
    # envelope sender into its Authentication-Results comment, and a sender
    # chooses its own local part and Message-ID.
    for column, value in (
        ("envelope_from", event.envelope_from),
        ("auth_result", event.auth_result),
        ("message_id", event.message_id),
    ):
        assert value is not None and "4111" not in value, (column, value)

    assert event.outcome == "failed"
    for entry in event.attachments:
        assert "4111" not in str(entry["name"]), entry
        assert "4111" not in str(entry.get("error", "")), entry
    assert event.attachments == r.json()["attachments"]

    # Past the event: the name the gate was handed carries no card number
    # either, so neither the batch row nor the filed record does.
    batch = _batches(db_session, "failed")[0]
    assert "4111" not in batch.source_file
    record = _error_records(tmp_path)[0]
    assert "4111" not in record.name
    assert "4111" not in record.read_text()
    _clean(tmp_path, raw, payload)


def test_a_message_is_partial_when_only_some_attachments_ingest(
    db_session, client, tmp_path
):
    """A mixed message is neither `ingested` nor `failed`: the operator needs
    to see that tonight's mail landed one report and lost another."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    good = FLASH.read_bytes()
    bad = b"%PDF-1.4 this is not a real pdf at all"
    raw = _message(attachments=[("flash.pdf", good), ("broken.pdf", bad)])

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "partial"
    assert [a["outcome"] for a in body["attachments"]] == ["ingested", "failed"]
    assert len(_batches(db_session, "transformed")) == 1
    assert len(_batches(db_session, "failed")) == 1
    assert len(_error_records(tmp_path)) == 1
    event = _events(db_session)[0]
    assert event.outcome == "partial"
    assert event.attachments == body["attachments"]
    _clean(tmp_path, raw, good, bad)


def test_the_recipient_domain_is_compared_case_insensitively(
    db_session, client, tmp_path
):
    """A receiver may hand the envelope recipient back in any case; the
    address still resolves."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    raw = _message()

    r = _post(client, raw, to=f"{HISJ_ADDRESS.upper()}@{_DOMAIN.upper()}")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "no_attachment"
    assert [e.outcome for e in _events(db_session)] == ["no_attachment"]
    _clean(tmp_path, raw)


def test_a_bracketed_recipient_resolves_like_a_bare_one(
    db_session, client, tmp_path
):
    """`<na-hisj@intake.example.test>` is the same address as the bare form.

    `intake._domain_of` already unwraps the SENDER, on the stated grounds that
    a worker forwarding the envelope verbatim can hand over `<gm@hotel.test>`.
    The recipient arrives by the same route and had no such unwrapping: the
    `>` rode on the domain and failed the compare, so the message answered
    200 `unknown_address` — which the worker does NOT forward to the fallback
    mailbox, so the night's report would have been lost with no event. The
    refusal below is therefore what a lost report looks like from here.
    """
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    raw = _message()

    r = _post(client, raw, to=f"<{HISJ_ADDRESS}@{_DOMAIN}>")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "no_attachment"
    assert [e.outcome for e in _events(db_session)] == ["no_attachment"]
    _clean(tmp_path, raw)


def test_a_bracketed_recipient_at_another_domain_is_still_refused(
    db_session, client, tmp_path
):
    """Unwrapping the brackets must not unwrap the domain check with them."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    raw = _message()

    r = _post(client, raw, to=f"<{HISJ_ADDRESS}@not-our-intake-domain.test>")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "unknown_address"
    assert _events(db_session) == []
    _clean(tmp_path, raw)


def test_a_long_message_id_is_clipped_to_the_column_width(
    db_session, client, tmp_path
):
    width = EmailIntakeEvent.__table__.c.message_id.type.length
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    raw = _message(message_id="<" + "m" * (width + 80) + "@pms.test>")

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    event = _events(db_session)[0]
    assert event.message_id is not None and len(event.message_id) == width
    _clean(tmp_path, raw)


def test_a_long_subject_is_clipped_to_the_column_width(db_session, client, tmp_path):
    """Postgres raises on overflow rather than truncating, so the writer clips
    to the width `models.EmailIntakeEvent` declares."""
    width = EmailIntakeEvent.__table__.c.subject.type.length
    raw = _message(subject="s" * (width + 100))
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")

    assert r.status_code == 200, r.text
    event = _events(db_session)[0]
    assert event.subject is not None and len(event.subject) == width
    _clean(tmp_path, raw)


def test_a_long_envelope_sender_and_auth_result_are_clipped(
    db_session, client, tmp_path
):
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    columns = EmailIntakeEvent.__table__.c
    long_local = "a" * (columns.envelope_from.type.length + 50)
    raw = _message()

    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}",
              frm=f"{long_local}@pms.test",
              auth="dkim=pass header.d=pms.test " + "x" * 2000)

    assert r.status_code == 200, r.text
    event = _events(db_session)[0]
    assert len(event.envelope_from) == columns.envelope_from.type.length
    assert event.auth_result is not None
    assert len(event.auth_result) == columns.auth_result.type.length
    _clean(tmp_path, raw)


def test_a_failed_attachment_can_be_resent(db_session, client, tmp_path):
    """Dedupe matches `transformed` batches only (D-OH23.6). A report that
    failed never landed, so a re-send is processed again and fails again —
    a corrupt nightly report repeats in the log until somebody fixes it,
    rather than going quiet as a `duplicate` of something that is not there.
    """
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    payload = b"%PDF-1.4 this is not a real pdf at all"
    raw = _message(attachments=[("broken.pdf", payload)])
    to = f"{HISJ_ADDRESS}@{_DOMAIN}"

    first = _post(client, raw, to=to)
    second = _post(client, raw, to=to)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["outcome"] == "failed"
    assert second.json()["outcome"] == "failed"
    assert second.json()["attachments"][0]["outcome"] == "failed"
    assert len(_batches(db_session, "failed")) == 2
    assert [e.outcome for e in _events(db_session)] == ["failed", "failed"]
    _clean(tmp_path, raw, payload)


def test_a_failure_outranks_a_duplicate_when_nothing_ingested(
    db_session, client, tmp_path
):
    """A message that re-sends last night's report beside one that will not
    parse must not read `duplicate` in the event log's outcome column."""
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    good = FLASH.read_bytes()
    bad = b"%PDF-1.4 this is not a real pdf at all"
    to = f"{HISJ_ADDRESS}@{_DOMAIN}"
    assert _post(client, _message(attachments=[("flash.pdf", good)]),
                 to=to).json()["outcome"] == "ingested"
    raw = _message(message_id="<two@pms.test>",
                   attachments=[("flash.pdf", good), ("broken.pdf", bad)])

    r = _post(client, raw, to=to)

    assert r.status_code == 200, r.text
    assert [a["outcome"] for a in r.json()["attachments"]] == ["duplicate", "failed"]
    assert r.json()["outcome"] == "failed"
    assert _events(db_session)[1].outcome == "failed"
    _clean(tmp_path, raw, good, bad)


def test_a_message_of_many_parts_is_bounded_by_the_ceiling(
    db_session, client, tmp_path, monkeypatch
):
    """The ceiling bounds ENUMERATION, not only ingestion.

    Before `attachments_of` took a `limit`, every part was decoded, hashed
    and given an entry of its own however many there were, so a message built
    of tens of thousands of six-byte parts bought seconds of blocked event
    loop and megabytes of response and JSONB. Now the walk stops at the
    ceiling plus one and the surplus is a single entry.
    """
    parts = [(f"part-{i}.pdf", b"%PDF-1." + str(i).encode()) for i in range(200)]
    raw = _message(attachments=parts)
    _seed(db_session, "opera")
    _address(db_session, HISJ_ADDRESS, "HISJ")
    # The slice in `_attachment_entries` hides an unbounded walk from every
    # assertion below — the entries come out the same either way — so the
    # limit the route ASKS FOR is recorded here. What that limit then does is
    # tests/test_intake.py::test_a_limit_stops_the_walk_at_the_ceiling_plus_one.
    asked: list[int | None] = []
    walk = intake_api.attachments_of

    def _record_limit(raw_bytes: bytes, *, limit: int | None = None):
        asked.append(limit)
        return walk(raw_bytes, limit=limit)

    monkeypatch.setattr(intake_api, "attachments_of", _record_limit)

    started = time.monotonic()
    r = _post(client, raw, to=f"{HISJ_ADDRESS}@{_DOMAIN}")
    elapsed = time.monotonic() - started

    assert r.status_code == 200, r.text
    entries = r.json()["attachments"]
    assert len(entries) == intake_api._MAX_ATTACHMENTS + 1
    assert entries[-1] == {"name": "(more attachments)", "sha256": "", "bytes": 0,
                           "outcome": "failed", "error": "too_many_attachments"}
    # Exactly the ceiling was opened: one failed batch and one error record
    # each, and nothing for the other 190 parts.
    assert len(_batches(db_session, "failed")) == intake_api._MAX_ATTACHMENTS
    assert len(_error_records(tmp_path)) == intake_api._MAX_ATTACHMENTS
    assert _events(db_session)[0].attachments == entries
    assert asked == [intake_api._MAX_ATTACHMENTS]
    # A smoke bound, not the pin: measured at 0.14 s for this message.
    assert elapsed < 2.0, f"{elapsed:.2f}s for a 200-part message"
    _clean(tmp_path, raw, *[data for _name, data in parts[:12]])


# --- tenancy ------------------------------------------------------------------


def test_an_address_on_another_orgs_property_ingests_into_that_org(
    db_session, two_tenant_world, app_role_engine, db_url, tmp_path
):
    """The address row resolves the tenant, and EVERYTHING after that lookup —
    the ingest and the event row — runs inside `OrgBoundSessionFactory(base,
    row.org_id)`. `email_intake_event` is OrgScoped with a server default of
    org 1 on org_id, so an event written on an unbound session would land in
    org 1 whatever address it names; org 1 seeing no event is what proves the
    binding. Org 1 holds HISJ under the same match phrase, so detection under
    org 2 has a decoy to get wrong."""
    _seed(db_session, "opera")  # HISJ under org 1, the decoy
    org2 = two_tenant_world.org2_id
    factory = make_session_factory(app_role_engine)
    with factory() as s:
        bind_org_context(s, org2)
        pid = create_first_property(
            s, org2, name="Holiday Inn & Suites San Jose", pms_source="opera")
        s.commit()
    _address(db_session, "na-orgtwo", pid, org_id=org2)

    # The serving app connects as the RLS-bound app role, so the database wall
    # applies as well as the ORM one.
    client = TestClient(_app(make_engine(app_role_url(db_url)), tmp_path))
    payload = FLASH.read_bytes()
    raw = _message(attachments=[("flash.pdf", payload)])

    r = _post(client, raw, to=f"na-orgtwo@{_DOMAIN}")

    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "ingested"
    with factory() as s:
        bind_org_context(s, org2)
        events = _events(s)
        assert [e.outcome for e in events] == ["ingested"]
        assert {e.org_id for e in events} == {org2}
        assert events[0].property_id == pid
        assert len(_batches(s, "transformed")) == 1
    with factory() as s:
        bind_org_context(s, 1)
        assert _events(s) == []
        assert s.scalar(select(func.count()).select_from(IngestBatch)) == 0
    _clean(tmp_path, raw, payload)
