# tests/test_preview.py
from datetime import date
from decimal import Decimal

from usali.preview import build_financial_preview
from usali.schemas import StagedRecord


def _rec(code: str, amount: str, desc: str | None = None) -> StagedRecord:
    return StagedRecord(
        property_id="PREVIEW",
        pms_source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pms_trx_code=code,
        pms_trx_desc=desc,
        raw_amount=Decimal(amount),
    )


def test_build_financial_preview_aggregates_and_counts():
    records = [
        _rec("1000", "5487.00"),
        _rec("9004", "-5487.00"),
        _rec("5105", "120.00"),
        _rec("8888", "0.00", desc="mystery line"),
    ]
    p = build_financial_preview(
        source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        records=records,
    )
    lines = {(line.major, line.line_item): line.amount for line in p.pnl_lines}
    assert lines[("Operated Departments", "Room Revenue")] == Decimal("5487.00")
    assert lines[("Settlements", "Visa")] == Decimal("-5487.00")
    assert p.codes_recognized == 4
    assert p.codes_mapped == 3
    assert p.codes_needs_review == 2
    assert p.net_total == Decimal("120.00")
    assert p.balanced is False


def test_balanced_true_when_net_zero():
    p = build_financial_preview(
        source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        records=[_rec("1000", "5487.00"), _rec("9004", "-5487.00")],
    )
    assert p.balanced is True
    assert p.net_total == Decimal("0")
