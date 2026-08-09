from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class StagedRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    property_id: str
    pms_source: str
    report_type: str
    business_date: date
    pms_trx_code: str
    pms_trx_desc: str | None = None
    raw_amount: Decimal
    room_count: int = 0
    section: str | None = None


class StatisticRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    property_id: str
    pms_source: str
    report_type: str
    business_date: date
    metric_label: str
    period_label: str
    is_prior_year: bool = False
    value: Decimal


class LedgerRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    property_id: str
    pms_source: str
    report_type: str
    business_date: date
    ledger_label: str
    kind: str  # "balance" | "activity"
    amount: Decimal


class SegmentRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    property_id: str
    pms_source: str
    report_type: str
    business_date: date
    segment_code: str
    segment_desc: str | None = None
    measure: str
    period_label: str
    value: Decimal
