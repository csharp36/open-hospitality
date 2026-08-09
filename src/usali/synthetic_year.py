"""A deterministic synthetic financial year for the cloud demo (K6b).

Every figure here is INVENTED. The cloud instance must be fictitious by
construction (Pillar K, decision 1) — no real report figure may reach
it — so the demo's financials come from this generator instead of the
committed sample PDFs. It emits the adaptors' own record shapes
(StagedRecord / StatisticRecord / SegmentRecord / LedgerRecord), so the
seed drives the REAL stage → transform/promote pipeline and every
report surface has a year to show.

Design constraints, in order:
- Deterministic: a given (property, date) always yields the same
  records, so re-running the seed is an exact no-op. The RNG is keyed
  on (property, date) — never on wall-clock anything.
- Mapped-only: transaction codes, stat labels, segment codes, and
  ledger labels come from the curated mapping YAMLs' vocabulary; a
  synthetic unmapped row would pollute the coverage report.
- Coherent: the trial balance, the statistics, and the segment mix all
  describe the SAME invented day — ADR × occupied = room revenue, the
  segment TOTAL rows carry the exact sums the strict segment promote
  reconciles against, and the Opera TB nets to zero (settlements
  exactly offset charges).
"""

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from usali.schemas import LedgerRecord, SegmentRecord, StagedRecord, StatisticRecord

PROPERTY_SOURCE = {"HISJ": "OPERA", "SSSJ": "AUTOCLERK"}

# The year is anchored, not relative — the demo world's other stories
# (the 2026-08-03 demand week, the punch narrative) sit just past it.
YEAR_END = date(2026, 7, 31)
YEAR_DAYS = 365

_CENT = Decimal("0.01")


def synthetic_dates() -> list[date]:
    return [YEAR_END - timedelta(days=i) for i in range(YEAR_DAYS)][::-1]


@dataclass(frozen=True)
class SyntheticDay:
    financial: list[StagedRecord]
    statistics: list[StatisticRecord]
    segments: list[SegmentRecord]
    ledgers: list[LedgerRecord]


# ---------------------------------------------------------------- the world

# Invented hotels: a 120-room select-service business hotel (HISJ) and a
# 62-room economy leisure property (SSSJ). Monthly occupancy shapes a
# San Jose year (summer peak, holiday trough); weekday shapes differ —
# the business hotel fills midweek, the economy property on weekends.
_ROOMS = {"HISJ": 120, "SSSJ": 62}

_OCC_MONTH = {
    "HISJ": {1: 0.58, 2: 0.63, 3: 0.67, 4: 0.71, 5: 0.74, 6: 0.81,
             7: 0.86, 8: 0.84, 9: 0.79, 10: 0.74, 11: 0.63, 12: 0.55},
    "SSSJ": {1: 0.52, 2: 0.56, 3: 0.61, 4: 0.66, 5: 0.70, 6: 0.78,
             7: 0.83, 8: 0.82, 9: 0.74, 10: 0.68, 11: 0.58, 12: 0.51},
}
_OCC_WEEKDAY = {  # Mon..Sun deltas
    "HISJ": (0.05, 0.06, 0.06, 0.04, -0.03, -0.07, -0.05),
    "SSSJ": (-0.03, -0.04, -0.03, -0.01, 0.05, 0.09, 0.02),
}
_ADR_MONTH = {
    "HISJ": {1: 152.0, 2: 156.0, 3: 161.0, 4: 168.0, 5: 172.0, 6: 184.0,
             7: 191.0, 8: 188.0, 9: 179.0, 10: 171.0, 11: 158.0, 12: 149.0},
    "SSSJ": {1: 94.0, 2: 96.0, 3: 99.0, 4: 104.0, 5: 108.0, 6: 116.0,
             7: 122.0, 8: 120.0, 9: 112.0, 10: 106.0, 11: 98.0, 12: 92.0},
}


@dataclass(frozen=True)
class _DayModel:
    available: int
    occupied: int
    room_revenue_cents: int
    arrivals: int
    departures: int
    no_shows: int
    in_house_persons: int


def _rng(property_id: str, business_date: date, stream: str) -> random.Random:
    return random.Random(f"usali-synth-v1:{property_id}:{business_date.isoformat()}:{stream}")


def _model(property_id: str, business_date: date) -> _DayModel:
    rng = _rng(property_id, business_date, "model")
    rooms = _ROOMS[property_id]
    ooo = rng.choice((0, 0, 0, 1, 1, 2, 3))
    available = rooms - ooo
    occ = (_OCC_MONTH[property_id][business_date.month]
           + _OCC_WEEKDAY[property_id][business_date.weekday()]
           + rng.uniform(-0.04, 0.04))
    occupied = max(int(round(available * min(max(occ, 0.30), 0.98))), 1)
    adr = _ADR_MONTH[property_id][business_date.month] + rng.uniform(-6.0, 6.0)
    room_revenue_cents = int(round(occupied * adr * 100))
    arrivals = int(round(occupied * rng.uniform(0.38, 0.52)))
    return _DayModel(
        available=available,
        occupied=occupied,
        room_revenue_cents=room_revenue_cents,
        arrivals=arrivals,
        departures=int(round(occupied * rng.uniform(0.36, 0.50))),
        no_shows=rng.choice((0, 0, 0, 1, 1, 2)),
        in_house_persons=int(round(occupied * rng.uniform(1.35, 1.65))),
    )


def _amount(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(_CENT)


def _split_cents(total: int, weights: list[float]) -> list[int]:
    """Allocate integer cents by weight; the remainder lands on the
    largest share so the parts ALWAYS sum exactly to the total."""
    scale = sum(weights)
    parts = [int(total * w / scale) for w in weights]
    parts[weights.index(max(weights))] += total - sum(parts)
    return parts


# ------------------------------------------------------------ OPERA (HISJ)

_OPERA_TAXES = (  # code, description, rate on room revenue
    ("5007", "Hotel Business Improvement District Fee", Decimal("0.009")),
    ("7100", "Transient Occupancy Tax 10%", Decimal("0.10")),
    ("7101", "CCFD 4%", Decimal("0.04")),
    ("7102", "CA Tourism 0.195%", Decimal("0.00195")),
)
_OPERA_SETTLEMENTS = (  # code, description, weight
    ("9004", "Visa", 0.42),
    ("9005", "MasterCard", 0.27),
    ("9003", "American Express", 0.17),
    ("9007", "Discover", 0.06),
    ("9002", "Direct Billing/City Ledger", 0.08),
)
_OPERA_SEGMENTS = (  # code, weight (D absorbs allocation remainders)
    ("D", 0.26), ("P", 0.18), ("L", 0.14), ("G", 0.10), ("W", 0.08),
    ("K", 0.08), ("J", 0.06), ("M", 0.04), ("N", 0.02),
)


def _opera_day(business_date: date, model: _DayModel) -> SyntheticDay:
    rng = _rng("HISJ", business_date, "opera")
    room = model.room_revenue_cents
    parking = int(room * rng.uniform(0.025, 0.045))
    gift_taxable = int(room * rng.uniform(0.004, 0.009))
    gift_nontax = int(gift_taxable * rng.uniform(0.2, 0.5))
    sales_tax = int(round((gift_taxable + parking) * 0.0938))

    fin: list[tuple[str, str, int]] = [
        ("1000", "*Accommodation", room),
        ("5105", "Parking", parking),
        ("5106", "Gift Shop Taxable", gift_taxable),
        ("5107", "Gift Shop Non Taxable", gift_nontax),
        ("7104", "Sale Tax %9.38", sales_tax),
    ]
    fin.extend(
        (code, desc, int(round(room * float(rate))))
        for code, desc, rate in _OPERA_TAXES
    )
    charges = sum(cents for _, _, cents in fin)
    settlements = _split_cents(charges, [w for _, _, w in _OPERA_SETTLEMENTS])
    fin.extend(
        (code, desc, -cents)
        for (code, desc, _), cents in zip(_OPERA_SETTLEMENTS, settlements)
    )
    section = {"1000": "Revenue", "5105": "Revenue", "5106": "Revenue", "5107": "Revenue"}
    financial = [
        StagedRecord(
            property_id="HISJ", pms_source="OPERA", report_type="trial_balance",
            business_date=business_date, pms_trx_code=code, pms_trx_desc=desc,
            raw_amount=_amount(cents),
            section=section.get(code, "Payment" if cents < 0 else "Non Revenue"),
        )
        for code, desc, cents in fin
    ]

    other_rev = _amount(parking + gift_taxable + gift_nontax)
    room_rev = _amount(room)
    occupied = Decimal(model.occupied)
    stats_values: list[tuple[str, Decimal]] = [
        ("% Rooms Occupied",
         (occupied * 100 / model.available).quantize(_CENT)),
        ("ADR", (room_rev / occupied).quantize(_CENT)),
        ("Revenue per Available Room minus OOO",
         (room_rev / model.available).quantize(_CENT)),
        ("Total Revenue", room_rev + other_rev),
        ("Room Revenue", room_rev),
        ("Food And Beverage Revenue", Decimal("0.00")),
        ("Other Revenue", other_rev),
        ("Total Rooms in Hotel", Decimal(_ROOMS["HISJ"])),
        ("Available Rooms minus OOO Rooms", Decimal(model.available)),
        ("Rooms Occupied", occupied),
        ("Arrival Rooms", Decimal(model.arrivals)),
        ("Departure Rooms", Decimal(model.departures)),
        ("No Show Rooms", Decimal(model.no_shows)),
        ("Total In-House Persons", Decimal(model.in_house_persons)),
    ]
    statistics = [
        StatisticRecord(
            property_id="HISJ", pms_source="OPERA", report_type="manager_flash",
            business_date=business_date, metric_label=label,
            period_label="DAY", value=value,
        )
        for label, value in stats_values
    ]

    segments = _segment_records(
        "HISJ", "OPERA", "market_stats", business_date,
        _OPERA_SEGMENTS, model.occupied, room,
    )
    return SyntheticDay(financial, statistics, segments,
                        _opera_ledgers(business_date, model))


def _ar_balance_cents(business_date: date) -> int:
    doy = business_date.timetuple().tm_yday
    return int(round((9000 + 4000 * math.sin(2 * math.pi * doy / 366)) * 100))


def _opera_ledgers(business_date: date, model: _DayModel) -> list[LedgerRecord]:
    rng = _rng("HISJ", business_date, "ledgers")
    guest = int(round(model.room_revenue_cents * rng.uniform(1.05, 1.35)))
    ar = _ar_balance_cents(business_date)
    ar_charges = int(round(model.room_revenue_cents * 0.08))
    ar_payments = -(ar_charges - (ar - _ar_balance_cents(business_date - timedelta(days=1))))
    deposit = int(round((1500 + 900 * math.sin(
        2 * math.pi * business_date.timetuple().tm_yday / 366)) * 100))
    package = int(round(rng.uniform(150, 420) * 100))
    rows = (
        ("Guest Ledger / Balance Today", "balance", guest),
        ("AR Ledger / Balance Today", "balance", ar),
        ("Deposit Ledger / Balance Today", "balance", deposit),
        ("Package Ledger / Balance Today", "balance", package),
        ("Hotel Balance", "balance", guest + ar + deposit + package),
        ("AR Ledger / Charges and Transfers", "activity", ar_charges),
        ("AR Ledger / Payments", "activity", ar_payments),
    )
    return [
        LedgerRecord(
            property_id="HISJ", pms_source="OPERA", report_type="trial_balance",
            business_date=business_date, ledger_label=label, kind=kind,
            amount=_amount(cents),
        )
        for label, kind, cents in rows
    ]


# --------------------------------------------------------- AUTOCLERK (SSSJ)

_AC_FEES = (  # code, description, (lo, hi) dollars — small incidental lines
    ("ROOM|EARLY_CHECK_IN", "Room - Early Check In", (0, 75)),
    ("ROOM|LATE_CHECK_OUT", "Room - Late Check Out", (0, 60)),
    ("ROOM|CANCELLATION_CHARGE", "Room - Cancellation Charge", (0, 130)),
    ("ROOM|NO_SHOW_CHARGE", "Room - No Show Charge", (0, 110)),
    ("MISC|PET_FEE", "Misc - PET FEE", (0, 80)),
    ("MISC|ELECTRIC_CHARGER", "Misc - Electric Charger", (0, 25)),
    ("PARKING|PARKING_FEES", "Parking - Parking Fees", (30, 140)),
    ("LAUNDRY|SOAP", "Laundry - Soap", (0, 15)),
    ("HIE_MARKET_SELL|WATER", "HIE Market Sell - Water", (5, 30)),
    ("HIE_MARKET_SELL|SODA", "HIE Market Sell - Soda", (5, 35)),
)
_AC_TAXES = (
    ("TAX|OCCUPANCY_TAX", "Tax - Occupancy Tax", Decimal("0.10")),
    ("TAX|COUNTY_TAX", "Tax - County Tax", Decimal("0.02")),
    ("TAX|TOURISM_FEE", "Tax - Tourism Fee", Decimal("0.01")),
)
_AC_CARDS = (  # negative settlements, weighted
    ("CREDIT_CARDS|VISA", "Credit Cards - Visa", 0.40),
    ("CREDIT_CARDS|MASTERCARD", "Credit Cards - Mastercard", 0.26),
    ("CREDIT_CARDS|AMERICAN_EXPRESS", "Credit Cards - American Express", 0.14),
    ("ACCOUNTS|DIRECT_BILL", "Accounts - Direct Bill", 0.12),
)
_AC_SEGMENTS = (
    ("BW", 0.24), ("RACK", 0.18), ("9Q", 0.14), ("BC5", 0.12),
    ("EC5", 0.10), ("GTL", 0.08), ("CLC", 0.10), ("FX2", 0.04),
)
_AC_SECTION = {  # section = the report heading, the text before " - "
    "ROOM": "Room", "TAX": "Tax", "CREDIT_CARDS": "Credit Cards",
    "ACCOUNTS": "Accounts", "MISC": "Misc", "LAUNDRY": "Laundry",
    "PARKING": "Parking", "HIE_MARKET_SELL": "HIE Market Sell",
    "CASH": "Cash",
}


def _autoclerk_day(business_date: date, model: _DayModel) -> SyntheticDay:
    rng = _rng("SSSJ", business_date, "autoclerk")
    room = model.room_revenue_cents
    fin: list[tuple[str, str, int]] = [("ROOM|ROOM_RENT", "Room - Room Rent", room)]
    fin.extend(
        (code, desc, int(round(rng.uniform(lo, hi) * 100)))
        for code, desc, (lo, hi) in _AC_FEES
    )
    fin.extend(
        (code, desc, int(round(room * float(rate))))
        for code, desc, rate in _AC_TAXES
    )
    charges = sum(cents for _, _, cents in fin)
    cash = int(charges * rng.uniform(0.03, 0.07))
    card_pool = charges - cash
    fin.append(("CASH|CASH", "Cash - Cash", cash))
    fin.extend(
        (code, desc, -cents)
        for (code, desc, _), cents in zip(
            _AC_CARDS, _split_cents(card_pool, [w for _, _, w in _AC_CARDS]))
    )
    financial = [
        StagedRecord(
            property_id="SSSJ", pms_source="AUTOCLERK",
            report_type="transaction_summary", business_date=business_date,
            pms_trx_code=code, pms_trx_desc=desc, raw_amount=_amount(cents),
            section=_AC_SECTION[code.split("|", 1)[0]],
        )
        for code, desc, cents in fin
    ]

    room_rev = _amount(room)
    occupied = Decimal(model.occupied)
    stats_values = [
        ("Occupancy - Occupied", occupied),
        ("Occupancy - Occupied Percent",
         (occupied * 100 / model.available).quantize(_CENT)),
        ("Occupancy - ADR", (room_rev / occupied).quantize(_CENT)),
        ("Occupancy - REVPAR", (room_rev / model.available).quantize(_CENT)),
        ("Occupancy - Total", Decimal(model.in_house_persons)),
        ("Statistics - Arrivals", Decimal(model.arrivals)),
        ("Statistics - Departures", Decimal(model.departures)),
        ("Statistics - In-House", occupied),
        ("Statistics - No Shows", Decimal(model.no_shows)),
    ]
    statistics = [
        StatisticRecord(
            property_id="SSSJ", pms_source="AUTOCLERK",
            report_type="manager_report", business_date=business_date,
            metric_label=label, period_label="Today", value=value,
        )
        for label, value in stats_values
    ]

    segments = _segment_records(
        "SSSJ", "AUTOCLERK", "rate_plan", business_date,
        _AC_SEGMENTS, model.occupied, room,
    )
    return SyntheticDay(financial, statistics, segments, [])


# ----------------------------------------------------------------- segments


def _segment_records(
    property_id: str,
    source: str,
    report_type: str,
    business_date: date,
    weights: tuple[tuple[str, float], ...],
    occupied: int,
    room_revenue_cents: int,
) -> list[SegmentRecord]:
    """Rooms and revenue split across the mapped mix, TOTAL rows exact —
    the strict segment promote reconciles parts against TOTAL, and the
    TOTAL figures are the SAME occupied/revenue the other shapes carry."""
    rng = _rng(property_id, business_date, "segments")
    jittered = [w * rng.uniform(0.6, 1.4) for _, w in weights]
    room_parts = _split_cents(occupied, jittered)
    rev_parts = _split_cents(room_revenue_cents, jittered)

    def rec(code: str, measure: str, value: Decimal) -> SegmentRecord:
        return SegmentRecord(
            property_id=property_id, pms_source=source, report_type=report_type,
            business_date=business_date, segment_code=code, measure=measure,
            period_label="DAY", value=value,
        )

    records = []
    for (code, _), rooms, rev in zip(weights, room_parts, rev_parts):
        records.append(rec(code, "ROOMS", Decimal(rooms)))
        records.append(rec(code, "ROOM_REVENUE", _amount(rev)))
    records.append(rec("TOTAL", "ROOMS", Decimal(occupied)))
    records.append(rec("TOTAL", "ROOM_REVENUE", _amount(room_revenue_cents)))
    return records


def synthetic_day(property_id: str, business_date: date) -> SyntheticDay:
    model = _model(property_id, business_date)
    if PROPERTY_SOURCE[property_id] == "OPERA":
        return _opera_day(business_date, model)
    return _autoclerk_day(business_date, model)
