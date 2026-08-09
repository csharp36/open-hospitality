"""CPA monthly pack (P8 Task 5): sales, tax, and A/R reports over one month.

Report depth against the six seeded sample PDFs (single business date
2026-07-07, so every MTD number equals the day and every movement is zero),
plus renderer/CSV shape and the CLI wiring. The sales-total invariant is
asserted the honest way: by actually summing per-day SOS TORs over the month.
"""

import csv
import io
import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from usali.cli import app
from usali.models import IngestBatch, PmsDailyFinancialStage, UsaliFinancialFact
from usali.render import (
    ar_report_csv,
    render_cpa_pack_json,
    render_cpa_pack_text,
    sales_report_csv,
    tax_report_csv,
)
from usali.reporting import cpa_pack, month_bounds, summary_operating_statement

runner = CliRunner()

MONTH = "2026-07"


# --- sales report ---------------------------------------------------------------


def test_sales_report_total_matches_per_day_sos_sum(db_session, seed_six_pdfs):
    pack = cpa_pack(db_session, property_id="HISJ", month=MONTH)

    assert pack.property_id == "HISJ"
    assert pack.pms_source == "OPERA"
    assert pack.month == MONTH
    assert pack.sales.total_operating_revenue == Decimal("10866.37")

    # Invariant: the monthly total equals the sum of the per-day SOS TORs.
    start, end = month_bounds(MONTH)
    days = sorted(
        set(
            db_session.scalars(
                select(UsaliFinancialFact.business_date)
                .where(
                    UsaliFinancialFact.property_id == "HISJ",
                    UsaliFinancialFact.business_date >= start,
                    UsaliFinancialFact.business_date <= end,
                )
                .distinct()
            )
        )
    )
    assert days == [date(2026, 7, 7)]  # seeded data is a single business date
    per_day_total = sum(
        (
            summary_operating_statement(
                db_session, property_id="HISJ", business_date=d
            ).total_operating_revenue
            for d in days
        ),
        Decimal("0"),
    )
    assert pack.sales.total_operating_revenue == per_day_total

    # Lines reconcile to the total, and every seeded line spans exactly one day.
    assert pack.sales.lines
    assert sum((line.mtd_amount for line in pack.sales.lines), Decimal("0")) == per_day_total
    assert all(line.day_count == 1 for line in pack.sales.lines)
    rooms_total = sum(
        (line.mtd_amount for line in pack.sales.lines if line.sub_category == "Rooms"),
        Decimal("0"),
    )
    assert rooms_total == Decimal("10395.00")


# --- tax report -----------------------------------------------------------------


def test_tax_report_totals_and_room_base(db_session, seed_six_pdfs):
    pack = cpa_pack(db_session, property_id="HISJ", month=MONTH)

    assert pack.taxes.taxes_total == Decimal("1573.29")
    assert pack.taxes.room_revenue_base == Decimal("10395.00")
    assert {line.line_item for line in pack.taxes.lines} == {
        "Hotel BID Fee",
        "Transient Occupancy Tax",
        "CCFD",
        "CA Tourism",
        "Sales Tax",
    }
    # All Opera tax codes are curated to the 2100 tax-liability GL account.
    assert all(line.gl_account_code == "2100" for line in pack.taxes.lines)
    assert (
        sum((line.mtd_amount for line in pack.taxes.lines), Decimal("0"))
        == pack.taxes.taxes_total
    )


# --- A/R report -----------------------------------------------------------------


def test_ar_report_balances_only(db_session, seed_six_pdfs):
    pack = cpa_pack(db_session, property_id="HISJ", month=MONTH)

    by_code = {line.ledger_code: line for line in pack.ar.balances}
    # kind=balance facts only — AR_CHARGES / AR_PAYMENTS (activity) never appear.
    assert set(by_code) == {
        "GUEST_LEDGER",
        "AR_LEDGER",
        "DEPOSIT_LEDGER",
        "PACKAGE_LEDGER",
        "HOTEL_BALANCE",
    }

    guest = by_code["GUEST_LEDGER"]
    assert guest.ledger_name == "Guest Ledger"
    assert guest.closing_balance == Decimal("11627.58")
    assert guest.opening_balance == guest.closing_balance  # single day in the month
    assert guest.movement == 0

    assert by_code["AR_LEDGER"].closing_balance == Decimal("19742.41")
    assert by_code["DEPOSIT_LEDGER"].closing_balance == Decimal("-2098.56")
    assert by_code["HOTEL_BALANCE"].closing_balance == Decimal("29271.43")


def test_ar_report_empty_for_property_without_ledger_facts(db_session, seed_six_pdfs):
    # SSSJ (Autoclerk) has financial facts but no ledger facts: the A/R report is
    # gracefully empty — not an error.
    pack = cpa_pack(db_session, property_id="SSSJ", month=MONTH)
    assert pack.pms_source == "AUTOCLERK"
    assert pack.ar.balances == []
    assert pack.sales.total_operating_revenue == Decimal("6359.03")


# --- edges ----------------------------------------------------------------------


def test_empty_month_is_a_clean_empty_pack(db_session, seed_six_pdfs):
    pack = cpa_pack(db_session, property_id="HISJ", month="2026-08")
    assert pack.sales.lines == []
    assert pack.sales.total_operating_revenue == 0
    assert pack.taxes.lines == []
    assert pack.taxes.taxes_total == 0
    assert pack.taxes.room_revenue_base == 0
    assert pack.ar.balances == []
    # The PMS source is still derived (property-wide) when the month is empty.
    assert pack.pms_source == "OPERA"


def test_unknown_property_is_a_loud_error(db_session, seed_six_pdfs):
    # A typo'd --property must never read as a plausible "no activity" zero pack.
    with pytest.raises(ValueError, match="unknown property"):
        cpa_pack(db_session, property_id="TYPO", month=MONTH)


def _synthetic_fact(db_session, *, pms_source, row_hash, property_id="TEST"):
    """Insert one financial fact directly (with its required batch + stage rows)."""
    batch = IngestBatch(
        pms_source=pms_source, report_type="trial_balance",
        source_file="synthetic.pdf", file_hash="0" * 64,
    )
    db_session.add(batch)
    db_session.flush()
    stage = PmsDailyFinancialStage(
        property_id=property_id, pms_source=pms_source, report_type="trial_balance",
        business_date=date(2026, 7, 7), pms_trx_code="9999", raw_amount=Decimal("1.00"),
        source_file="synthetic.pdf", ingest_batch_id=batch.batch_id, row_hash=row_hash,
    )
    db_session.add(stage)
    db_session.flush()
    db_session.add(UsaliFinancialFact(
        property_id=property_id, pms_source=pms_source, business_date=date(2026, 7, 7),
        usali_edition=12, usali_schedule_id=1,
        usali_major_category="Synthetic", usali_sub_category="Synthetic",
        usali_line_item="Synthetic", amount=Decimal("1.00"),
        ingest_batch_id=batch.batch_id, stage_id=stage.stage_id,
    ))
    db_session.flush()


def test_multi_source_property_is_rejected(db_session, founding_org):
    _synthetic_fact(db_session, pms_source="OPERA", row_hash="b" * 64)
    _synthetic_fact(db_session, pms_source="AUTOCLERK", row_hash="c" * 64)

    with pytest.raises(ValueError, match="multiple PMS sources"):
        cpa_pack(db_session, property_id="TEST", month=MONTH)


def test_bad_month_string_is_a_clean_error(db_session):
    for bad in ("2026-13", "202607", "2026-7", "garbage", "2026-07-07"):
        with pytest.raises(ValueError):
            cpa_pack(db_session, property_id="HISJ", month=bad)


# --- renderers ------------------------------------------------------------------


def test_text_and_json_renderers(db_session, seed_six_pdfs):
    pack = cpa_pack(db_session, property_id="HISJ", month=MONTH)

    text = render_cpa_pack_text(pack)
    assert "CPA MONTHLY PACK" in text
    assert "SALES REPORT" in text
    assert "TAX REPORT" in text
    assert "A/R LEDGER BALANCES" in text
    assert "GUEST_LEDGER" in text

    data = json.loads(render_cpa_pack_json(pack))
    assert data["report"] == "cpa_pack"
    assert data["property_id"] == "HISJ"
    assert data["month"] == MONTH
    assert Decimal(data["sales_report"]["total_operating_revenue"]) == Decimal("10866.37")
    assert Decimal(data["tax_report"]["taxes_total"]) == Decimal("1573.29")
    assert Decimal(data["tax_report"]["room_revenue_base"]) == Decimal("10395.00")
    balances = {b["ledger_code"]: b for b in data["ar_report"]["balances"]}
    assert Decimal(balances["GUEST_LEDGER"]["closing_balance"]) == Decimal("11627.58")
    assert Decimal(balances["GUEST_LEDGER"]["movement"]) == 0


def test_csv_reports_parse_with_spot_values(db_session, seed_six_pdfs):
    pack = cpa_pack(db_session, property_id="HISJ", month=MONTH)

    sales_rows = list(csv.reader(io.StringIO(sales_report_csv(pack))))
    assert sales_rows[0] == ["major", "sub_category", "line_item", "mtd_amount", "day_count"]
    assert sales_rows[-1][0] == "TOTAL_OPERATING_REVENUE"
    assert Decimal(sales_rows[-1][3]) == Decimal("10866.37")
    assert all(row[4] == "1" for row in sales_rows[1:-1])

    tax_rows = list(csv.reader(io.StringIO(tax_report_csv(pack))))
    assert tax_rows[0] == ["line_item", "gl_account_code", "mtd_amount"]
    assert tax_rows[-2][0] == "TAXES_TOTAL"
    assert Decimal(tax_rows[-2][2]) == Decimal("1573.29")
    assert tax_rows[-1][0] == "ROOM_REVENUE_BASE"
    assert Decimal(tax_rows[-1][2]) == Decimal("10395.00")

    ar_rows = list(csv.reader(io.StringIO(ar_report_csv(pack))))
    # Chronological field order (opening first) — frozen; downstream mirrors it.
    assert ar_rows[0] == [
        "ledger_code",
        "ledger_name",
        "opening_balance",
        "closing_balance",
        "movement",
    ]
    guest = next(row for row in ar_rows if row[0] == "GUEST_LEDGER")
    assert Decimal(guest[2]) == Decimal("11627.58")  # opening (single day: == closing)
    assert Decimal(guest[3]) == Decimal("11627.58")  # closing
    assert Decimal(guest[4]) == 0  # movement


# --- CLI wiring -----------------------------------------------------------------


def test_cli_cpa_pack(db_session, seed_six_pdfs, tmp_path):
    # text to stdout
    result = runner.invoke(app, ["cpa-pack", "--property", "HISJ", "--month", MONTH])
    assert result.exit_code == 0, result.output
    assert "CPA MONTHLY PACK" in result.output

    # json single stream into --out DIR as cpa-pack.json
    json_dir = tmp_path / "pack-json"
    result = runner.invoke(
        app,
        ["cpa-pack", "--property", "HISJ", "--month", MONTH,
         "--format", "json", "--out", str(json_dir)],
    )
    assert result.exit_code == 0, result.output
    data = json.loads((json_dir / "cpa-pack.json").read_text())
    assert Decimal(data["tax_report"]["taxes_total"]) == Decimal("1573.29")

    # text into --out DIR as cpa-pack.txt
    txt_dir = tmp_path / "pack-txt"
    result = runner.invoke(
        app, ["cpa-pack", "--property", "HISJ", "--month", MONTH, "--out", str(txt_dir)]
    )
    assert result.exit_code == 0, result.output
    assert "CPA MONTHLY PACK" in (txt_dir / "cpa-pack.txt").read_text()

    # csv REQUIRES --out
    result = runner.invoke(
        app, ["cpa-pack", "--property", "HISJ", "--month", MONTH, "--format", "csv"]
    )
    assert result.exit_code != 0
    assert "--out" in result.output + result.stderr

    # csv writes the three per-report files into --out DIR
    csv_dir = tmp_path / "pack-csv"
    result = runner.invoke(
        app,
        ["cpa-pack", "--property", "HISJ", "--month", MONTH,
         "--format", "csv", "--out", str(csv_dir)],
    )
    assert result.exit_code == 0, result.output
    for name in ("sales_report.csv", "tax_report.csv", "ar_report.csv"):
        rows = list(csv.reader(io.StringIO((csv_dir / name).read_text())))
        assert rows and rows[0]  # header present and parseable
    sales_rows = list(csv.reader(io.StringIO((csv_dir / "sales_report.csv").read_text())))
    assert Decimal(sales_rows[-1][3]) == Decimal("10866.37")

    # typo'd property → FAILED on stderr + exit 1 (never a plausible zero pack)
    result = runner.invoke(app, ["cpa-pack", "--property", "TYPO", "--month", MONTH])
    assert result.exit_code == 1
    assert "unknown property" in result.output + result.stderr


def test_cli_cpa_pack_rejects_bad_month_and_format():
    result = runner.invoke(app, ["cpa-pack", "--property", "HISJ", "--month", "2026-13"])
    assert result.exit_code != 0
    assert "YYYY-MM" in result.output + result.stderr

    result = runner.invoke(
        app, ["cpa-pack", "--property", "HISJ", "--month", MONTH, "--format", "xml"]
    )
    assert result.exit_code != 0
