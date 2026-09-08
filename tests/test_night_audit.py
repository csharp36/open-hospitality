"""Night-audit flow: state init, checklist, ledger checks, roll gating."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from tests.authkit import DEFAULT_ORG_ALIAS, make_authkit
from tests.grants import grant_role
from usali.db import make_session_factory
from usali.keycloak_admin import InMemoryKeycloakAdmin
from usali.models import NightAuditState, Organization, Property
from usali.night_audit import (
    get_or_init_state,
    ledger_checks,
    roll_window,
    slot_status,
)
from usali.server import create_app


def _client(db_engine, tmp_path, verifier) -> TestClient:
    app = create_app(
        inbox_dir=tmp_path / "inbox", processed_dir=tmp_path / "processed",
        failed_dir=tmp_path / "failed",
        session_factory=make_session_factory(db_engine),
        token_verifier=verifier, keycloak_admin=InMemoryKeycloakAdmin(),
    )
    return TestClient(app)


def _org_and_property(db_session, pid="HISJ", pms_source="OPERA"):
    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.add(Property(property_id=pid, org_id=1, name=pid, pms_source=pms_source))
    db_session.commit()


def _admin_headers(mint, db_session):
    grant_role(db_session, "org_admin", sub="na-admin", org_id=1)
    tok = mint(roles=["org_admin"], sub="na-admin")
    return {"Authorization": f"Bearer {tok}"}


def _seed_ledger_facts(db_session, facts, pid="HISJ", tag="lf"):
    """Promote (code, kind, amount, on) rows as OPERA trial-balance ledger facts."""
    from usali.models import IngestBatch, PmsLedgerBalanceStage, UsaliLedgerBalanceFact

    batch = IngestBatch(pms_source="OPERA", report_type="trial_balance",
                        source_file=f"{tag}.pdf", file_hash=f"{tag}-h")
    db_session.add(batch)
    db_session.flush()
    for code, kind, amount, on in facts:
        stage = PmsLedgerBalanceStage(
            property_id=pid, pms_source="OPERA", report_type="trial_balance",
            business_date=on, ledger_label=code, kind=kind, amount=amount,
            source_file=f"{tag}.pdf", row_hash=f"{tag}-{code}-{on}",
            ingest_batch_id=batch.batch_id,
        )
        db_session.add(stage)
        db_session.flush()
        db_session.add(UsaliLedgerBalanceFact(
            property_id=pid, pms_source="OPERA", business_date=on,
            ledger_code=code, ledger_name=code, kind=kind, amount=amount,
            ingest_batch_id=batch.batch_id, ledger_stage_id=stage.ledger_stage_id,
        ))
    db_session.commit()


def _passing_ledger_set(day):
    """A balanced identity plus an exactly-tying signed roll-forward:
    200 + 15 + (-5) == 210."""
    prior = day - timedelta(days=1)
    return [
        ("GUEST_LEDGER", "balance", Decimal("100"), day),
        ("AR_LEDGER", "balance", Decimal("210"), day),
        ("DEPOSIT_LEDGER", "balance", Decimal("-10"), day),
        ("PACKAGE_LEDGER", "balance", Decimal("0"), day),
        ("HOTEL_BALANCE", "balance", Decimal("300"), day),
        ("AR_CHARGES", "activity", Decimal("15"), day),
        ("AR_PAYMENTS", "activity", Decimal("-5"), day),
        ("AR_LEDGER", "balance", Decimal("200"), prior),
    ]


def _freeze_window(monkeypatch, *, open_now):
    """Pin the api module's roll_window; real boundary behavior is pinned by
    test_roll_window_boundaries."""
    import usali.night_audit_api as api

    monkeypatch.setattr(
        api, "roll_window",
        lambda prop, now=None: {"open": open_now, "hours": "00:00–05:00",
                                "timezone": prop.timezone,
                                "local_time": "01:00" if open_now else "08:00"},
    )


# ---- service ---------------------------------------------------------------


def test_state_initializes_after_last_fact(db_session):
    """A property with facts through D starts its night audit at D+1 — D's
    audit already happened, by definition."""
    from usali.models import IngestBatch, PmsDailyFinancialStage, UsaliFinancialFact

    _org_and_property(db_session)
    batch = IngestBatch(pms_source="OPERA", report_type="trial_balance",
                        source_file="x.pdf", file_hash="h1")
    db_session.add(batch)
    db_session.flush()
    stage = PmsDailyFinancialStage(
        property_id="HISJ", pms_source="OPERA", report_type="trial_balance",
        business_date=date(2026, 8, 17), pms_trx_code="1000",
        raw_amount=Decimal("100"), source_file="x.pdf",
        ingest_batch_id=batch.batch_id, row_hash="rh1",
    )
    db_session.add(stage)
    db_session.flush()
    db_session.add(UsaliFinancialFact(
        property_id="HISJ", pms_source="OPERA", business_date=date(2026, 8, 17),
        usali_edition=12, usali_schedule_id=None,
        usali_major_category="Operated Departments", usali_sub_category="Rooms",
        usali_line_item="Room Revenue", amount=Decimal("100"),
        ingest_batch_id=batch.batch_id, stage_id=stage.stage_id,
    ))
    db_session.commit()

    prop = db_session.get(Property, "HISJ")
    state = get_or_init_state(db_session, prop)
    assert state.current_business_date == date(2026, 8, 18)


def test_state_for_empty_property_is_local_today(db_session):
    _org_and_property(db_session, pid="EMPTY1")
    prop = db_session.get(Property, "EMPTY1")
    state = get_or_init_state(db_session, prop)
    # Property-local business date (04:00 cutoff) — cannot pin an absolute
    # value, but it must be within a day of now.
    assert abs((state.current_business_date - datetime.now(UTC).date()).days) <= 1


def test_slot_status_tracks_coverage(db_session):
    from usali.ingestion import record_coverage

    _org_and_property(db_session)
    day = date(2026, 8, 18)
    record_coverage(db_session, "HISJ", day, "trial_balance")
    db_session.commit()
    slots = slot_status(db_session, "HISJ", day, "OPERA")
    assert [(s["report_type"], s["landed"]) for s in slots] == [
        ("trial_balance", True), ("manager_flash", False), ("market_stats", False),
    ]


def test_ledger_checks_pass_and_fail(db_session):
    from usali.models import IngestBatch, PmsLedgerBalanceStage, UsaliLedgerBalanceFact

    _org_and_property(db_session)
    day = date(2026, 8, 18)
    batch = IngestBatch(pms_source="OPERA", report_type="trial_balance",
                        source_file="x.pdf", file_hash="h2")
    db_session.add(batch)
    db_session.flush()

    def _fact(code, name, kind, amount, on=day):
        stage = PmsLedgerBalanceStage(
            property_id="HISJ", pms_source="OPERA", report_type="trial_balance",
            business_date=on, ledger_label=name, kind=kind, amount=amount,
            source_file="x.pdf", row_hash=f"{code}-{on}-{amount}",
            ingest_batch_id=batch.batch_id,
        )
        db_session.add(stage)
        db_session.flush()
        db_session.add(UsaliLedgerBalanceFact(
            property_id="HISJ", pms_source="OPERA", business_date=on,
            ledger_code=code, ledger_name=name, kind=kind, amount=amount,
            ingest_batch_id=batch.batch_id, ledger_stage_id=stage.ledger_stage_id,
        ))

    # Balanced identity + a consistent AR roll-forward from the prior day.
    # Payments carry the report's own minus sign into the fact table -- the
    # parser preserves signs (test_sign_opposed_duplicate_pair_kept_distinct_by_
    # heading in test_ledger_capture.py) and promote_ledgers copies amounts
    # verbatim -- so a NONZERO payment here is Decimal("-5"), not 5.
    _fact("GUEST_LEDGER", "Guest Ledger", "balance", Decimal("100"))
    _fact("AR_LEDGER", "AR / City Ledger", "balance", Decimal("210"))
    _fact("DEPOSIT_LEDGER", "Deposit Ledger", "balance", Decimal("-10"))
    _fact("PACKAGE_LEDGER", "Package Ledger", "balance", Decimal("0"))
    _fact("HOTEL_BALANCE", "Hotel Balance", "balance", Decimal("300"))
    _fact("AR_CHARGES", "AR Charges", "activity", Decimal("15"))
    _fact("AR_PAYMENTS", "AR Payments", "activity", Decimal("-5"))
    _fact("AR_LEDGER", "AR / City Ledger", "balance", Decimal("200"),
          on=date(2026, 8, 17))
    db_session.commit()

    checks = {c.name: c for c in ledger_checks(db_session, "HISJ", day, "OPERA")}
    assert checks["balance_identity"].status == "pass"
    assert checks["ar_rollforward"].status == "pass"  # 200 + 15 + (-5) == 210
    assert checks["ar_rollforward"].delta == "0.00"  # exact, not just within tolerance

    # Break the identity: raise the hotel balance without moving the subs.
    from sqlalchemy import update
    db_session.execute(
        update(UsaliLedgerBalanceFact)
        .where(UsaliLedgerBalanceFact.ledger_code == "HOTEL_BALANCE")
        .values(amount=Decimal("999"))
    )
    db_session.commit()
    checks = {c.name: c for c in ledger_checks(db_session, "HISJ", day, "OPERA")}
    assert checks["balance_identity"].status == "fail"


def test_ledger_checks_skip_without_data(db_session):
    _org_and_property(db_session, pid="SSSJ", pms_source="AUTOCLERK")
    checks = ledger_checks(db_session, "SSSJ", date(2026, 8, 18), "AUTOCLERK")
    assert [c.status for c in checks] == ["skipped"]


def test_opera_empty_ledger_block_fails_once_trial_balance_lands(db_session):
    """LEDGER_BLOCK_REPORT lists OPERA's trial balance as the ledger-block
    carrier: once it has landed, zero parsed ledger balances is a parser
    regression (layout drift), and 'skipped' would let the roll through
    unverified. Before it lands, the skip stays honest."""
    from usali.ingestion import record_coverage

    _org_and_property(db_session)
    day = date(2026, 8, 18)
    checks = ledger_checks(db_session, "HISJ", day, "OPERA")
    assert [c.status for c in checks] == ["skipped"]

    record_coverage(db_session, "HISJ", day, "trial_balance")
    db_session.commit()
    checks = ledger_checks(db_session, "HISJ", day, "OPERA")
    assert [(c.name, c.status) for c in checks] == [("ledger_block", "fail")]
    assert "ledger block" in checks[0].detail


def test_roll_window_is_property_local():
    prop = Property(property_id="X", org_id=1, name="X", pms_source="OPERA",
                    timezone="America/Los_Angeles")
    # 10:00 UTC = 03:00 PDT -> open;  15:00 UTC = 08:00 PDT -> closed.
    assert roll_window(prop, datetime(2026, 8, 18, 10, 0, tzinfo=UTC))["open"] is True
    assert roll_window(prop, datetime(2026, 8, 18, 15, 0, tzinfo=UTC))["open"] is False


def test_roll_window_boundaries():
    """00:00 is inside the window (inclusive open); 05:00 is outside
    (exclusive close), while 04:59:59 is still inside."""
    prop = Property(property_id="X", org_id=1, name="X", pms_source="OPERA",
                    timezone="UTC")
    assert roll_window(prop, datetime(2026, 8, 17, 23, 59, 59, tzinfo=UTC))["open"] is False
    assert roll_window(prop, datetime(2026, 8, 18, 0, 0, 0, tzinfo=UTC))["open"] is True
    assert roll_window(prop, datetime(2026, 8, 18, 4, 59, 59, tzinfo=UTC))["open"] is True
    assert roll_window(prop, datetime(2026, 8, 18, 5, 0, 0, tzinfo=UTC))["open"] is False


# ---- endpoints -------------------------------------------------------------


def test_get_night_audit_state(db_session, db_engine, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    r = client.get("/api/properties/HISJ/night-audit", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pms_source"] == "OPERA"
    assert [s["report_type"] for s in body["slots"]] == [
        "trial_balance", "manager_flash", "market_stats"]
    assert body["all_reports_landed"] is False
    assert body["can_roll"] is False


def test_roll_refuses_with_missing_reports(db_session, db_engine, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    r = client.post("/api/properties/HISJ/night-audit/roll", headers=headers)
    assert r.status_code == 409
    assert "awaiting" in r.json()["detail"]


def test_roll_refuses_outside_window_and_rolls_inside(
    db_session, db_engine, tmp_path, monkeypatch
):
    from usali.ingestion import record_coverage
    import usali.night_audit_api as api

    _org_and_property(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    # Land all three reports for the (lazily initialized) current date.
    r = client.get("/api/properties/HISJ/night-audit", headers=headers)
    day = date.fromisoformat(r.json()["business_date"])
    for rt in ("trial_balance", "manager_flash", "market_stats"):
        record_coverage(db_session, "HISJ", day, rt)
    db_session.commit()
    # A landed trial balance with no parsed ledger balances is a FAIL
    # (test_opera_empty_ledger_block_fails_once_trial_balance_lands) — seed a
    # green set so only the window stands between this test and the roll.
    _seed_ledger_facts(db_session, _passing_ledger_set(day))

    # Freeze the window: closed (08:00 property-local = 15:00 UTC in August).
    monkeypatch.setattr(
        api, "roll_window",
        lambda prop, now=None: {"open": False, "hours": "00:00–05:00",
                                "timezone": prop.timezone, "local_time": "08:00"},
    )
    r = client.post("/api/properties/HISJ/night-audit/roll", headers=headers)
    assert r.status_code == 409
    assert "roll window" in r.json()["detail"]

    # Open the window: the roll advances the date and audits.
    monkeypatch.setattr(
        api, "roll_window",
        lambda prop, now=None: {"open": True, "hours": "00:00–05:00",
                                "timezone": prop.timezone, "local_time": "01:00"},
    )
    r = client.post("/api/properties/HISJ/night-audit/roll", headers=headers)
    assert r.status_code == 200, r.text
    # The roll's own response already shows the ADVANCED date.
    assert r.json()["business_date"] == (day + timedelta(days=1)).isoformat()
    db_session.expire_all()
    state = db_session.get(NightAuditState, "HISJ")
    assert (state.current_business_date - day).days == 1

    from sqlalchemy import select
    from usali.models import AuditEvent
    events = db_session.execute(
        select(AuditEvent.action).where(AuditEvent.resource_id == "HISJ")
    ).scalars().all()
    assert "night_audit_rolled" in events


def test_roll_refuses_on_failing_ledger_check(db_session, db_engine, tmp_path, monkeypatch):
    """All reports landed, window open, but a ledger check FAILS: the roll must
    409 naming the check and advance nothing."""
    from usali.ingestion import record_coverage

    _org_and_property(db_session)
    day = date(2026, 8, 18)
    db_session.add(NightAuditState(property_id="HISJ", current_business_date=day))
    for rt in ("trial_balance", "manager_flash", "market_stats"):
        record_coverage(db_session, "HISJ", day, rt)
    # Break the identity: hotel balance disagrees with the four sub-ledgers.
    facts = [
        ("HOTEL_BALANCE", "balance", Decimal("999"), on) if code == "HOTEL_BALANCE"
        else (code, kind, amount, on)
        for code, kind, amount, on in _passing_ledger_set(day)
    ]
    _seed_ledger_facts(db_session, facts)

    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)
    _freeze_window(monkeypatch, open_now=True)
    r = client.post("/api/properties/HISJ/night-audit/roll", headers=headers)
    assert r.status_code == 409
    assert "balance_identity" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.get(NightAuditState, "HISJ").current_business_date == day


def test_roll_refuses_on_failing_segment_reconciliation(
    db_session, db_engine, tmp_path, monkeypatch
):
    """All reports landed, ledger checks green, window open, but the market-code
    table does not tie: the roll must 409 naming the reconciliation and advance
    nothing."""
    from sqlalchemy import update
    from usali.ingestion import record_coverage
    from usali.models import PmsDailySegmentStage

    day = _segment_world(db_session)
    # Break the table: D rooms 35 -> 32, so the code sum (59) misses flash (62).
    db_session.execute(update(PmsDailySegmentStage)
                       .where(PmsDailySegmentStage.segment_code == "D",
                              PmsDailySegmentStage.measure == "ROOMS")
                       .values(value=Decimal("32")))
    for rt in ("trial_balance", "manager_flash", "market_stats"):
        record_coverage(db_session, "HISJ", day, rt)
    # Green ledger checks, so only the segments gate stands between us and 200.
    _seed_ledger_facts(db_session, _passing_ledger_set(day))

    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)
    _freeze_window(monkeypatch, open_now=True)
    r = client.post("/api/properties/HISJ/night-audit/roll", headers=headers)
    assert r.status_code == 409
    assert "market-code reconciliation" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.get(NightAuditState, "HISJ").current_business_date == day


def test_opera_empty_ledger_block_blocks_roll(db_session, db_engine, tmp_path, monkeypatch):
    """Everything landed but NO ledger balances parsed (the F4 regression
    shape): the ledger_block FAIL must block the roll, not slide through as a
    skip."""
    from usali.ingestion import record_coverage

    _org_and_property(db_session)
    day = date(2026, 8, 18)
    db_session.add(NightAuditState(property_id="HISJ", current_business_date=day))
    for rt in ("trial_balance", "manager_flash", "market_stats"):
        record_coverage(db_session, "HISJ", day, rt)
    db_session.commit()

    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)
    _freeze_window(monkeypatch, open_now=True)
    r = client.post("/api/properties/HISJ/night-audit/roll", headers=headers)
    assert r.status_code == 409
    assert "ledger_block" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.get(NightAuditState, "HISJ").current_business_date == day


def test_autoclerk_absent_ledger_block_stays_skipped_and_rollable(
    db_session, db_engine, tmp_path, monkeypatch
):
    """AUTOCLERK is not in LEDGER_BLOCK_REPORT: a full night with no ledger
    balances keeps the honest skip and the roll proceeds."""
    from usali.ingestion import record_coverage

    _org_and_property(db_session, pid="SSSJ", pms_source="AUTOCLERK")
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    r = client.get("/api/properties/SSSJ/night-audit", headers=headers)
    day = date.fromisoformat(r.json()["business_date"])
    for rt in ("transaction_summary", "manager_report", "rate_plan"):
        record_coverage(db_session, "SSSJ", day, rt)
    db_session.commit()
    checks = ledger_checks(db_session, "SSSJ", day, "AUTOCLERK")
    assert [c.status for c in checks] == ["skipped"]

    _freeze_window(monkeypatch, open_now=True)
    r = client.post("/api/properties/SSSJ/night-audit/roll", headers=headers)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(NightAuditState, "SSSJ").current_business_date == day + timedelta(days=1)


def test_upload_rejects_wrong_business_date(db_session, db_engine, tmp_path):
    """The shipped 2026-07-07 sample must be refused when the current business
    date is later — and nothing may be staged by the refusal."""
    from pathlib import Path
    from sqlalchemy import func, select
    from usali.mapping.property_registry import seed_properties
    from usali.mapping.schedules import seed_schedules
    from usali.models import PmsDailyFinancialStage

    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.commit()
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    # Pin the state ahead of the sample's date.
    db_session.add(NightAuditState(property_id="HISJ",
                                   current_business_date=date(2026, 8, 18)))
    db_session.commit()

    sample = Path("docs/reference/samples/Trial Balance 07.07.2026 - Opera.pdf")
    r = client.post(
        "/api/properties/HISJ/night-audit/upload", headers=headers,
        files={"file": (sample.name, sample.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 422, r.text
    assert "current business date" in r.json()["detail"]
    staged = db_session.execute(
        select(func.count()).select_from(PmsDailyFinancialStage)
    ).scalar_one()
    assert staged == 0


def test_roles_gate_writes(db_session, db_engine, tmp_path):
    _org_and_property(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    grant_role(db_session, "accountant", sub="na-acct", org_id=1)
    tok = mint(roles=["accountant"], sub="na-acct")
    headers = {"Authorization": f"Bearer {tok}"}
    r = client.post("/api/properties/HISJ/night-audit/roll", headers=headers)
    assert r.status_code == 403


# ---- pack mode (SkyTouch) --------------------------------------------------


def _seed_world(db_session):
    from usali.mapping.loader import load_mappings
    from usali.mapping.property_registry import seed_properties
    from usali.mapping.schedules import seed_schedules

    db_session.merge(Organization(org_id=1, kc_org_alias=DEFAULT_ORG_ALIAS, name="Org"))
    db_session.commit()
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/skytouch.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()


def test_pack_mode_announced_for_skytouch(db_session, db_engine, tmp_path):
    _seed_world(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)
    r = client.get("/api/properties/STDEMO/night-audit", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload_mode"] == "pack"
    assert "Audit Pack" in body["pack_label"]
    assert [s["report_type"] for s in body["slots"]] == [
        "hotel_journal", "hotel_statistics"]
    # Opera stays per-report.
    r = client.get("/api/properties/HISJ/night-audit", headers=headers)
    assert r.json()["upload_mode"] == "reports"


def test_pack_upload_fills_both_slots(db_session, db_engine, tmp_path):
    from pathlib import Path

    _seed_world(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    pack = Path("docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf")
    # Pin the state to the pack's own business date.
    db_session.add(NightAuditState(property_id="STDEMO",
                                   current_business_date=date(2026, 6, 21)))
    db_session.commit()

    r = client.post(
        "/api/properties/STDEMO/night-audit/upload", headers=headers,
        files={"file": (pack.name, pack.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    landed = {s["report_type"]: s["landed"] for s in body["slots"]}
    assert landed == {"hotel_journal": True, "hotel_statistics": True}
    assert body["all_reports_landed"] is True
    kinds = {s["report_type"] for s in body["sections"] if not s["skipped"]}
    assert kinds == {"hotel_journal", "hotel_statistics"}
    # The A/R Aging filler is named and marked skipped, not silently dropped.
    assert any(s["skipped"] for s in body["sections"])


def test_pack_upload_rejects_wrong_business_date(db_session, db_engine, tmp_path):
    from pathlib import Path
    from sqlalchemy import func, select
    from usali.models import PmsDailyFinancialStage

    _seed_world(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    pack = Path("docs/reference/samples/SkyTouch - Standard Audit Pack (mock).pdf")
    db_session.add(NightAuditState(property_id="STDEMO",
                                   current_business_date=date(2026, 8, 23)))
    db_session.commit()

    r = client.post(
        "/api/properties/STDEMO/night-audit/upload", headers=headers,
        files={"file": (pack.name, pack.read_bytes(), "application/pdf")},
    )
    assert r.status_code == 422, r.text
    assert "current business date" in r.json()["detail"]
    staged = db_session.execute(
        select(func.count()).select_from(PmsDailyFinancialStage)
    ).scalar_one()
    assert staged == 0


# ---- direct-edit adjustment (cross-night correction) -----------------------


def _ledger_world(db_session):
    """HISJ with a failing AR roll-forward: today's signed activity implies a
    prior close of 20000.00 (19742.41 - 149.75 + 407.34), but 19000.00 is
    stored. Payments are signed-negative, as the staged rows are (the parser
    keeps the report's minus: test_sign_opposed_duplicate_pair_kept_distinct_
    by_heading in test_ledger_capture.py)."""
    from usali.models import IngestBatch, PmsLedgerBalanceStage, UsaliLedgerBalanceFact

    _org_and_property(db_session)
    batch = IngestBatch(pms_source="OPERA", report_type="trial_balance",
                        source_file="x.pdf", file_hash="adj-h")
    db_session.add(batch)
    db_session.flush()

    def _fact(code, name, kind, amount, on):
        stage = PmsLedgerBalanceStage(
            property_id="HISJ", pms_source="OPERA", report_type="trial_balance",
            business_date=on, ledger_label=name, kind=kind, amount=amount,
            source_file="x.pdf", row_hash=f"adj-{code}-{on}",
            ingest_batch_id=batch.batch_id,
        )
        db_session.add(stage)
        db_session.flush()
        db_session.add(UsaliLedgerBalanceFact(
            property_id="HISJ", pms_source="OPERA", business_date=on,
            ledger_code=code, ledger_name=name, kind=kind, amount=amount,
            ingest_batch_id=batch.batch_id, ledger_stage_id=stage.ledger_stage_id,
        ))

    today = date(2026, 7, 7)
    _fact("AR_LEDGER", "AR / City Ledger", "balance", Decimal("19742.41"), today)
    _fact("AR_CHARGES", "AR Charges", "activity", Decimal("149.75"), today)
    _fact("AR_PAYMENTS", "AR Payments", "activity", Decimal("-407.34"), today)
    _fact("AR_LEDGER", "AR / City Ledger", "balance", Decimal("19000.00"),
          date(2026, 7, 6))
    db_session.add(NightAuditState(property_id="HISJ", current_business_date=today))
    db_session.commit()


def test_adjust_fixes_rollforward_and_records_everything(db_session, db_engine, tmp_path):
    from sqlalchemy import select
    from usali.models import AuditEvent, NightAuditAdjustment

    _ledger_world(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    # Failing before, with the adjust affordance present and correctly computed.
    r = client.get("/api/properties/HISJ/night-audit", headers=headers)
    check = next(c for c in r.json()["verification"] if c["name"] == "ar_rollforward")
    assert check["status"] == "fail"
    assert check["delta"] == "-1000.00"  # 19000 + 149.75 + (-407.34) - 19742.41
    assert check["adjust"]["stored"] == "19000.00"  # money: 2dp everywhere
    # The suggestion must BE the true prior close -- the value under which
    # prior + charges + payments == today exactly -- because it is what the
    # adjust endpoint writes into the promoted fact.
    assert check["adjust"]["suggested"] == "20000.00"

    r = client.post(
        "/api/properties/HISJ/night-audit/adjust", headers=headers,
        json={"corrected_amount": "20000.00", "reason": "late city-ledger transfer"},
    )
    assert r.status_code == 200, r.text
    check = next(c for c in r.json()["verification"] if c["name"] == "ar_rollforward")
    assert check["status"] == "pass"
    assert check["adjust"] is None

    db_session.expire_all()
    adj = db_session.execute(select(NightAuditAdjustment)).scalar_one()
    assert str(adj.old_amount) == "19000.0000"
    assert str(adj.new_amount) == "20000.0000"
    assert adj.reason == "late city-ledger transfer"
    events = db_session.execute(
        select(AuditEvent.action).where(AuditEvent.resource_id == "HISJ")
    ).scalars().all()
    assert "night_audit_balance_adjusted" in events


def test_adjust_requires_reason_and_prior_close(db_session, db_engine, tmp_path):
    _ledger_world(db_session)
    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    # Reason too short -> pydantic 422, nothing changed.
    r = client.post("/api/properties/HISJ/night-audit/adjust", headers=headers,
                    json={"corrected_amount": "20000.00", "reason": "x"})
    assert r.status_code == 422

    # No prior close on file -> 409 names the skipped state.
    _org_and_property(db_session, pid="BARE1")
    db_session.add(NightAuditState(property_id="BARE1",
                                   current_business_date=date(2026, 7, 7)))
    db_session.commit()
    r = client.post("/api/properties/BARE1/night-audit/adjust", headers=headers,
                    json={"corrected_amount": "1.00", "reason": "should refuse"})
    assert r.status_code == 409
    assert "nothing to correct" in r.json()["detail"]


# ---- market-code reconciliation (tabular, RAW report codes) ----------------


def _segment_world(db_session):
    """HISJ 07-07 at the raw level: codes D=35/6047.33 and Y=27/4347.67 with a
    consistent TOTAL row (62/10395.00); flash total 62; trial-balance Rooms
    10395. D maps to TRANSIENT, Y to CONTRACT (mapping/segments.yaml)."""
    from usali.models import (
        IngestBatch, PmsDailyFinancialStage, PmsDailySegmentStage,
        PmsDailyStatisticStage, UsaliFinancialFact, UsaliStatisticFact,
    )

    _org_and_property(db_session)
    day = date(2026, 7, 7)
    batch = IngestBatch(pms_source="OPERA", report_type="trial_balance",
                        source_file="x.pdf", file_hash="seg-h")
    db_session.add(batch)
    db_session.flush()
    fin_stage = PmsDailyFinancialStage(
        property_id="HISJ", pms_source="OPERA", report_type="trial_balance",
        business_date=day, pms_trx_code="1000", raw_amount=Decimal("10395.00"),
        source_file="x.pdf", ingest_batch_id=batch.batch_id, row_hash="seg-fin",
    )
    db_session.add(fin_stage)
    db_session.flush()
    db_session.add(UsaliFinancialFact(
        property_id="HISJ", pms_source="OPERA", business_date=day,
        usali_edition=12, usali_schedule_id=1,
        usali_major_category="Operated Departments", usali_sub_category="Rooms",
        usali_line_item="Room Revenue", amount=Decimal("10395.00"),
        ingest_batch_id=batch.batch_id, stage_id=fin_stage.stage_id,
    ))
    stat_stage = PmsDailyStatisticStage(
        property_id="HISJ", pms_source="OPERA", report_type="manager_flash",
        business_date=day, metric_label="Rooms Occupied", period_label="DAY",
        is_prior_year=False, value=Decimal("62"), source_file="x.pdf",
        ingest_batch_id=batch.batch_id, row_hash="seg-stat",
    )
    db_session.add(stat_stage)
    db_session.flush()
    db_session.add(UsaliStatisticFact(
        property_id="HISJ", pms_source="OPERA", business_date=day,
        metric_code="ROOMS_OCCUPIED", period="DAY", is_prior_year=False,
        value=Decimal("62"), ingest_batch_id=batch.batch_id,
        stat_stage_id=stat_stage.stat_stage_id,
    ))
    rows = [
        ("D", "Discount - D", "ROOMS", "35"), ("D", "Discount - D", "ROOM_REVENUE", "6047.33"),
        ("Y", "Long-Term - Y", "ROOMS", "27"), ("Y", "Long-Term - Y", "ROOM_REVENUE", "4347.67"),
        ("TOTAL", None, "ROOMS", "62"), ("TOTAL", None, "ROOM_REVENUE", "10395.00"),
    ]
    for code, desc, measure, value in rows:
        db_session.add(PmsDailySegmentStage(
            property_id="HISJ", pms_source="OPERA", report_type="market_stats",
            business_date=day, segment_code=code, segment_desc=desc,
            measure=measure, period_label="DAY", value=Decimal(value),
            source_file="x.pdf", ingest_batch_id=batch.batch_id,
            row_hash=f"seg-{code}-{measure}",
        ))
    db_session.add(NightAuditState(property_id="HISJ", current_business_date=day))
    db_session.commit()
    return day


def test_segment_reconciliation_pass_fail_skipped(db_session):
    from sqlalchemy import update
    from usali.models import PmsDailySegmentStage
    from usali.night_audit import segment_reconciliation

    day = _segment_world(db_session)
    r = segment_reconciliation(db_session, "HISJ", day, "OPERA")
    assert r["status"] == "pass"
    assert [row["code"] for row in r["rows"]] == ["D", "Y"]
    assert r["rows"][0]["description"] == "Discount"  # " - D" suffix stripped
    assert r["report_total_rooms"] == "62"

    db_session.execute(update(PmsDailySegmentStage)
                       .where(PmsDailySegmentStage.segment_code == "D",
                              PmsDailySegmentStage.measure == "ROOMS")
                       .values(value=Decimal("32")))
    db_session.commit()
    r = segment_reconciliation(db_session, "HISJ", day, "OPERA")
    assert r["status"] == "fail" and r["rooms_delta"] == "-3"

    assert segment_reconciliation(db_session, "HISJ", day, "SKYTOUCH") is None
    assert segment_reconciliation(
        db_session, "HISJ", date(2026, 1, 1), "OPERA")["status"] == "skipped"


def test_segments_save_updates_stage_and_repromotes(db_session, db_engine, tmp_path):
    from sqlalchemy import select, update
    from usali.models import NightAuditAdjustment, PmsDailySegmentStage, UsaliSegmentFact

    day = _segment_world(db_session)
    # Break D rooms at the stage level (32 instead of 35): sum 59 vs flash 62,
    # and the report TOTAL row (62) now disagrees with the codes too.
    db_session.execute(update(PmsDailySegmentStage)
                       .where(PmsDailySegmentStage.segment_code == "D",
                              PmsDailySegmentStage.measure == "ROOMS")
                       .values(value=Decimal("32")))
    db_session.commit()

    verifier, mint = make_authkit()
    client = _client(db_engine, tmp_path, verifier)
    headers = _admin_headers(mint, db_session)

    r = client.get("/api/properties/HISJ/night-audit", headers=headers)
    assert r.json()["segments"]["status"] == "fail"
    assert r.json()["can_roll"] is False

    r = client.post(
        "/api/properties/HISJ/night-audit/segments", headers=headers,
        json={"rows": [
            {"code": "D", "rooms": "35", "room_revenue": "6047.33"},
            {"code": "Y", "rooms": "27", "room_revenue": "4347.67"},
        ]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["segments"]["status"] == "pass"

    db_session.expire_all()
    # stage corrected + logged
    adj = db_session.execute(
        select(NightAuditAdjustment)
        .where(NightAuditAdjustment.ledger_code == "MKT:D:ROOMS")
    ).scalar_one()
    assert str(adj.old_amount) == "32.0000" and str(adj.new_amount) == "35.0000"
    # facts re-promoted through the strict path: D -> TRANSIENT 35, Y -> CONTRACT 27
    facts = {
        f.usali_segment: str(f.rooms)
        for f in db_session.execute(
            select(UsaliSegmentFact).where(
                UsaliSegmentFact.property_id == "HISJ",
                UsaliSegmentFact.business_date == day,
                UsaliSegmentFact.period == "DAY",
            )
        ).scalars()
    }
    assert facts == {"TRANSIENT": "35.0000", "CONTRACT": "27.0000"}

    # Unknown code refused.
    r = client.post(
        "/api/properties/HISJ/night-audit/segments", headers=headers,
        json={"rows": [{"code": "NOPE", "rooms": "1", "room_revenue": "1"}]},
    )
    assert r.status_code == 422


# --- registry drift guards (review follow-ups) -------------------------------


def test_every_required_report_has_a_business_date_extractor():
    """REQUIRED_REPORTS and _DATE_FNS are two dicts, in two modules, that must
    agree. The upload endpoint's core guarantee is "business date == the current
    business date"; it can only check that if the report type has an extractor.

    Adding a report type to REQUIRED_REPORTS without one used to drop that check
    SILENTLY for the new type. The endpoint now refuses instead, and this test is
    what keeps that refusal unreachable.
    """
    from usali.night_audit import REQUIRED_REPORTS
    from usali.night_audit_api import _DATE_FNS

    missing = [
        (source, report_type)
        for source, reports in REQUIRED_REPORTS.items()
        for report_type, _label in reports
        if (source, report_type) not in _DATE_FNS
    ]
    assert not missing, f"no _DATE_FNS business-date extractor for: {missing}"


def test_ledger_reads_are_keyed_by_pms_source(db_session):
    """A property-day can hold ledger facts from two sources: the fact table's
    uniqueness is (property_id, pms_source, business_date, ledger_code), so a PMS
    migration or an /ingest backfill produces exactly that.

    Unfiltered, `_balances` kept whichever row the database returned last, and
    `ledger_checks` gates the ROLL on the result. Plant a decoy from a second
    source whose AR close would BREAK the identity if it were read, and prove the
    property's own source is the one that counts.
    """
    from usali.models import IngestBatch, PmsLedgerBalanceStage, UsaliLedgerBalanceFact
    from usali.night_audit import _balances

    _org_and_property(db_session)
    day = date(2026, 8, 18)

    def _fact(source: str, code: str, amount: Decimal) -> None:
        batch = IngestBatch(pms_source=source, report_type="trial_balance",
                            source_file=f"{source}-{code}.pdf", file_hash=f"h-{source}-{code}")
        db_session.add(batch)
        db_session.flush()
        stage = PmsLedgerBalanceStage(
            property_id="HISJ", pms_source=source, report_type="trial_balance",
            business_date=day, ledger_label=code, kind="balance", amount=amount,
            source_file=f"{source}.pdf", row_hash=f"r-{source}-{code}",
            ingest_batch_id=batch.batch_id,
        )
        db_session.add(stage)
        db_session.flush()
        db_session.add(UsaliLedgerBalanceFact(
            property_id="HISJ", pms_source=source, business_date=day,
            ledger_code=code, ledger_name=code, kind="balance", amount=amount,
            ingest_batch_id=batch.batch_id, ledger_stage_id=stage.ledger_stage_id,
        ))

    _fact("OPERA", "AR_LEDGER", Decimal("210"))
    # Same property, same day, same ledger — a different source. Permitted by
    # the unique constraint, and the value a collapsed read might pick up.
    _fact("AUTOCLERK", "AR_LEDGER", Decimal("999999"))
    db_session.flush()

    assert _balances(db_session, "HISJ", day, "OPERA")["AR_LEDGER"] == Decimal("210")
    assert _balances(db_session, "HISJ", day, "AUTOCLERK")["AR_LEDGER"] == Decimal("999999")
