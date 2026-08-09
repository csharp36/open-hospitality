import json
import logging
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from usali.adaptors.opera_trial_balance import parse_trial_balance, parse_trial_balance_ledgers
from usali.adaptors.pdf import Word
from usali.detect import Detection
from usali.ingestion import ProcessingError, _run_opera_trial_balance, process_file
from usali.ledger_promote import promote_ledgers
from usali.ledger_stage import stage_ledgers
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.models import (
    IngestBatch,
    PmsDailyFinancialStage,
    PmsLedgerBalanceStage,
    UsaliLedgerBalanceFact,
)
from usali.schemas import LedgerRecord

BUSINESS_DATE = date(2026, 7, 7)
SAMPLE = "Trial Balance 07.07.2026 - Opera.pdf"


def _load_words() -> list[Word]:
    data = json.loads(Path("tests/fixtures/opera_trial_balance_words.json").read_text())
    return [Word(text=d["text"], x0=d["x0"], top=d["top"]) for d in data]


def _parse() -> list[LedgerRecord]:
    return parse_trial_balance_ledgers(
        _load_words(), property_id="HISJ", business_date=BUSINESS_DATE
    )


# --- parser -------------------------------------------------------------------


def test_golden_ledger_rows():
    by_label = {r.ledger_label: r for r in _parse()}

    guest = by_label["Guest Ledger Balance"]
    assert guest.amount == Decimal("11627.58")
    assert guest.kind == "balance"

    assert by_label["AR Ledger Payments"].amount == Decimal("0.00")
    assert by_label["AR Ledger Payments"].kind == "activity"
    assert by_label["Deposit Ledger Activity"].amount == Decimal("0.00")

    assert by_label["AR Ledger / Balance Today"].amount == Decimal("19742.41")
    assert by_label["Deposit Ledger / Balance Today"].amount == Decimal("-2098.56")
    assert by_label["Package Ledger / Balance Today"].amount == Decimal("0.00")

    hotel = by_label["Hotel Balance"]  # summary row — must NOT inherit "Package Ledger /"
    assert hotel.amount == Decimal("29271.43")
    assert hotel.kind == "balance"


def test_sign_opposed_duplicate_pair_kept_distinct_by_heading():
    # The same label appears under two ledger headings with opposite signs. The heading
    # is part of the row's identity — both rows must survive with their own sign.
    by_label = {r.ledger_label: r for r in _parse()}
    guest_side = by_label["Guest Ledger / Deposits Transfered at Check-In"]
    deposit_side = by_label["Deposit Ledger / Deposits Transfered at Check-In"]
    assert guest_side.amount == Decimal("-1934.14")
    assert deposit_side.amount == Decimal("1934.14")
    assert guest_side.kind == deposit_side.kind == "activity"


def test_full_ledger_block_no_noise_or_furniture():
    labels = [r.ledger_label for r in _parse()]
    assert labels == [
        "Grand Total",
        "AR Ledger Payments",
        "Deposit Ledger Activity",
        "Balance Carried Forward",
        "Accruals Today",
        "Guest Ledger Balance",
        "Guest Ledger / Balance Yesterday",
        "Guest Ledger / Charges",
        "Guest Ledger / Payments",
        "Guest Ledger / Deposits Transfered at Check-In",
        "Guest Ledger / Balance Today",
        "AR Ledger / Balance Yesterday",
        "AR Ledger / Charges and Transfers",
        "AR Ledger / Payments",
        "AR Ledger / Balance Today",
        "Deposit Ledger / Balance Yesterday",
        "Deposit Ledger / Deposits Transfered at Check-In",
        "Deposit Ledger / Payments",
        "Deposit Ledger / Balance Today",
        "Package Ledger / Balance Yesterday",
        "Package Ledger / Packages Created Today",
        "Package Ledger / Packages Consumed Today",
        "Package Ledger / Balance Actual",
        "Package Ledger / Accruals Future",
        "Package Ledger / Balance Today",
        "Hotel Balance",
    ]


def test_record_shape_and_kinds():
    records = _parse()
    assert records
    assert all(r.pms_source == "OPERA" and r.report_type == "trial_balance" for r in records)
    assert all(r.property_id == "HISJ" and r.business_date == BUSINESS_DATE for r in records)
    assert {r.kind for r in records} == {"balance", "activity"}
    assert all(("Balance" in r.ledger_label) == (r.kind == "balance") for r in records)


def test_financial_parse_unchanged_by_ledger_capture():
    # Ledger rows never matched CODE_RE, so the financial parser output for this report
    # must be identical to what it was before ledger capture existed: 14 coded rows.
    records = parse_trial_balance(
        _load_words(), property_id="HISJ", business_date=BUSINESS_DATE
    )
    assert len(records) == 14
    assert all(r.pms_trx_code.isdigit() for r in records)


# --- staging ------------------------------------------------------------------


def _batch(db_session) -> IngestBatch:
    batch = IngestBatch(
        pms_source="OPERA",
        report_type="trial_balance",
        source_file="tb.pdf",
        file_hash="fh",
        status="staged",
        row_count=0,
    )
    db_session.add(batch)
    db_session.flush()
    return batch


def test_stage_ledgers_is_idempotent(db_session, founding_org):
    records = _parse()
    batch = _batch(db_session)
    stage_ledgers(db_session, records, batch=batch, source_file="tb.pdf", file_hash="dup")
    db_session.commit()
    stage_ledgers(db_session, records, batch=batch, source_file="tb.pdf", file_hash="dup")
    db_session.commit()
    assert db_session.scalar(
        select(func.count()).select_from(PmsLedgerBalanceStage)
    ) == len(records)


def test_stage_keeps_both_sign_opposed_rows(db_session, founding_org):
    records = _parse()
    batch = _batch(db_session)
    stage_ledgers(db_session, records, batch=batch, source_file="tb.pdf", file_hash="fh")
    db_session.commit()
    amounts = db_session.execute(
        select(PmsLedgerBalanceStage.ledger_label, PmsLedgerBalanceStage.amount).where(
            PmsLedgerBalanceStage.ledger_label.like("%Deposits Transfered at Check-In")
        )
    ).all()
    assert sorted(a for _, a in amounts) == [Decimal("-1934.1400"), Decimal("1934.1400")]


# --- promote ------------------------------------------------------------------


def test_promote_stamps_curated_kind_and_warns_on_disagreement(db_session, founding_org, tmp_path, caplog):
    # Facts must carry the CURATED kind (the A/R report filters on it), not the parser's
    # "label contains 'Balance'" heuristic; a disagreement is surfaced as a warning.
    batch = _batch(db_session)
    stage_ledgers(db_session, _parse(), batch=batch, source_file="tb.pdf", file_hash="fh")
    db_session.commit()

    mapping = tmp_path / "ledgers.yaml"
    mapping.write_text(
        '- {source: OPERA, label: "Guest Ledger / Charges", code: GUEST_CHARGES,'
        ' name: "Guest Charges", kind: balance}\n'  # heuristic says activity
    )
    # Alembic's fileConfig (run once by the db fixture) disables pre-existing loggers;
    # re-enable ours so the warning is observable here.
    logging.getLogger("usali.ledger_promote").disabled = False
    with caplog.at_level(logging.WARNING, logger="usali.ledger_promote"):
        result = promote_ledgers(
            db_session, mapping, source="OPERA", business_date=BUSINESS_DATE
        )
    db_session.commit()

    assert result.promoted == 1
    fact = db_session.execute(select(UsaliLedgerBalanceFact)).scalar_one()
    assert fact.kind == "balance"  # curated kind wins on the fact
    assert "kind disagreement" in caplog.text


# --- end to end ---------------------------------------------------------------


def _ingest_sample(db_session, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir(exist_ok=True)
    shutil.copy(Path("docs/reference/samples") / SAMPLE, inbox / SAMPLE)
    process_file(
        db_session, inbox / SAMPLE,
        processed_dir=tmp_path / "processed", failed_dir=tmp_path / "failed",
    )


def test_trial_balance_end_to_end_ledger_facts(db_session, tmp_path):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    _ingest_sample(db_session, tmp_path)

    # Financial staging is untouched by ledger capture — same 14 coded rows as always.
    assert db_session.scalar(select(func.count()).select_from(PmsDailyFinancialStage)) == 14
    assert db_session.scalar(select(func.count()).select_from(PmsLedgerBalanceStage)) == 26

    facts = db_session.execute(select(UsaliLedgerBalanceFact)).scalars().all()
    by_code = {f.ledger_code: f for f in facts}
    guest = by_code["GUEST_LEDGER"]
    assert guest.amount == Decimal("11627.5800")
    assert guest.property_id == "HISJ"
    assert guest.business_date == BUSINESS_DATE
    assert guest.kind == "balance"
    assert by_code["AR_LEDGER"].amount == Decimal("19742.4100")
    assert by_code["DEPOSIT_LEDGER"].amount == Decimal("-2098.5600")
    assert by_code["PACKAGE_LEDGER"].amount == Decimal("0.0000")
    assert by_code["HOTEL_BALANCE"].amount == Decimal("29271.4300")
    assert by_code["AR_CHARGES"].kind == by_code["AR_PAYMENTS"].kind == "activity"

    # Exactly the 7 curated labels promote; unmapped labels stay staged, informational.
    assert len(facts) == 7
    assert all(f.ledger_stage_id is not None for f in facts)


def test_reingest_is_idempotent(db_session, tmp_path):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    _ingest_sample(db_session, tmp_path)
    stage_before = db_session.scalar(select(func.count()).select_from(PmsLedgerBalanceStage))
    fact_before = db_session.scalar(select(func.count()).select_from(UsaliLedgerBalanceFact))
    fin_before = db_session.scalar(select(func.count()).select_from(PmsDailyFinancialStage))

    _ingest_sample(db_session, tmp_path)  # same bytes → same row hashes → dedup
    assert db_session.scalar(
        select(func.count()).select_from(PmsLedgerBalanceStage)
    ) == stage_before == 26
    assert db_session.scalar(
        select(func.count()).select_from(UsaliLedgerBalanceFact)
    ) == fact_before
    assert db_session.scalar(
        select(func.count()).select_from(PmsDailyFinancialStage)
    ) == fin_before == 14


def test_reexport_of_same_date_fails_loud_not_double_counted(db_session, tmp_path):
    # A corrected night-audit re-export has different bytes → different file_hash →
    # fresh row hashes → everything re-stages and re-promotes. The fact-level unique
    # constraint (property, source, date, ledger_code) must reject the duplicate facts
    # loudly through the quarantine path instead of double-counting the A/R report.
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/opera.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()

    _ingest_sample(db_session, tmp_path)
    fact_before = db_session.scalar(select(func.count()).select_from(UsaliLedgerBalanceFact))

    src = Path("docs/reference/samples") / SAMPLE
    tweaked = tmp_path / "inbox" / SAMPLE
    tweaked.write_bytes(src.read_bytes() + b"\n% re-export")  # same content, new hash
    with pytest.raises(ProcessingError):
        process_file(
            db_session, tweaked,
            processed_dir=tmp_path / "processed", failed_dir=tmp_path / "failed",
        )

    assert (tmp_path / "failed" / SAMPLE).exists()  # quarantined, not filed as processed
    assert db_session.scalar(
        select(func.count()).select_from(UsaliLedgerBalanceFact)
    ) == fact_before == 7  # rolled back — no double counting
    failed = db_session.execute(
        select(IngestBatch).where(IngestBatch.status == "failed")
    ).scalars().all()
    assert len(failed) == 1


def test_missing_ledger_block_fails_loud(db_session, founding_org):
    # If Opera ever renames the block anchor ("Transaction Total Today"), ledger capture
    # must fail the whole file loudly instead of silently staging zero ledger rows.
    words = _load_words()
    anchor_top = min(w.top for w in words if w.text == "Transaction")
    truncated = [w for w in words if w.top < anchor_top - 1.0]  # cut at the anchor row

    det = Detection(pms_source="OPERA", report_type="trial_balance", property_id="HISJ")
    with pytest.raises(ValueError, match="ledger reconciliation block"):
        _run_opera_trial_balance(
            db_session, truncated, det, Path("tb.pdf"), "fh", 12
        )
