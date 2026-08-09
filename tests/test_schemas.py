from datetime import date
from decimal import Decimal

from usali.schemas import StagedRecord


def test_staged_record_roundtrip():
    r = StagedRecord(
        property_id="HISJ",
        pms_source="OPERA",
        report_type="trial_balance",
        business_date=date(2026, 7, 7),
        pms_trx_code="1000",
        pms_trx_desc="*Accommodation",
        raw_amount=Decimal("10395.00"),
        section="Revenue",
    )
    assert r.room_count == 0
    assert r.raw_amount == Decimal("10395.00")
