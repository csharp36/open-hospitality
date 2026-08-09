"""SOS renderers (P6 Task 2): text, JSON, and CSV from a synthetic report.

No database — a hand-built `SosReport` exercises every section, both date modes,
and the prior-year statistics toggle. Assertions target structure and key values,
not byte-for-byte golden output.
"""

import csv
import io
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

from usali.render import render_sos_csv, render_sos_json, render_sos_text
from usali.reporting import DeptSection, MetricRow, SegmentLine, SosLine, SosReport

_STATS_WITH_PRIOR = [
    MetricRow(
        metric_code="ADR",
        day=Decimal("207.88"),
        mtd=Decimal("201.10"),
        ytd=Decimal("195.00"),
        day_prior=Decimal("199.50"),
        mtd_prior=None,
        ytd_prior=Decimal("190.00"),
    ),
    MetricRow(
        metric_code="OCC_PCT",
        day=Decimal("83.3"),
        mtd=Decimal("79.1"),
        ytd=Decimal("75.5"),
        day_prior=Decimal("80.0"),
        mtd_prior=Decimal("78.0"),
        ytd_prior=Decimal("74.0"),
    ),
]

_STATS_NO_PRIOR = [
    MetricRow(
        metric_code="OCC_PCT",
        day=Decimal("83.3"),
        mtd=Decimal("79.1"),
        ytd=Decimal("75.5"),
        day_prior=None,
        mtd_prior=None,
        ytd_prior=None,
    ),
]


def build_sos() -> SosReport:
    """Synthetic single-date Opera-style report covering every section."""
    rooms_lines = [
        SosLine("Rooms Revenue", "Rooms", "Group Rooms Revenue", Decimal("1395.00")),
        SosLine("Rooms Revenue", "Rooms", "Transient Rooms Revenue", Decimal("9000.00")),
    ]
    fb_lines = [
        SosLine("F&B Revenue", "Food & Beverage", "Breakfast Revenue", Decimal("61.37")),
    ]
    return SosReport(
        property_id="HISJ",
        pms_source="OPERA",
        business_date=date(2026, 7, 7),
        date_from=None,
        date_to=None,
        operated_departments=[
            DeptSection("Rooms", rooms_lines, Decimal("10395.00")),
            DeptSection("Food & Beverage", fb_lines, Decimal("61.37")),
        ],
        misc_income=[
            SosLine("Miscellaneous Income", "Parking", "Parking", Decimal("410.00")),
        ],
        misc_income_total=Decimal("410.00"),
        total_operating_revenue=Decimal("10866.37"),
        taxes=[
            SosLine("Taxes (Pass-Through)", "Taxes", "Occupancy Tax", Decimal("1000.00")),
            SosLine("Taxes (Pass-Through)", "Taxes", "State Sales Tax", Decimal("573.29")),
        ],
        taxes_total=Decimal("1573.29"),
        settlements=[
            SosLine("Settlements", "Settlements", "Credit Card Settlement", Decimal("-7093.48")),
        ],
        settlements_total=Decimal("-7093.48"),
        other=[],
        other_total=Decimal("0.00"),
        rooms_segments=[
            SegmentLine("GROUP", Decimal("10"), Decimal("2079.00")),
            SegmentLine("TRANSIENT", Decimal("40"), Decimal("8316.00")),
        ],
        statistics=_STATS_WITH_PRIOR,
        payroll_expense=[],
        payroll_expense_total=Decimal("0"),
        labor_hours_total=Decimal("0"),
        labor_ot_hours_total=Decimal("0"),
        labor_fte=None,
        labor_suppressed_departments=0,
        labor_unpriced_hours=Decimal("0"),
        labor_variance=None,
    )


def build_sos_range() -> SosReport:
    """Same report in range mode (July 2026)."""
    return replace(
        build_sos(),
        business_date=None,
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 31),
    )


# --- text -------------------------------------------------------------------


def test_text_sections_and_totals() -> None:
    text = render_sos_text(build_sos())

    for header in (
        "SUMMARY OPERATING STATEMENT",
        "OPERATED DEPARTMENTS",
        "MISCELLANEOUS INCOME",
        "TOTAL OPERATING REVENUE",
        "ROOMS SEGMENT SPLIT",
        "TAXES COLLECTED",
        "SETTLEMENTS",
        "STATISTICS",
    ):
        assert header in text

    assert "Property: HISJ" in text
    assert "PMS source: OPERA" in text
    assert "Business date: 2026-07-07" in text
    assert "expenses live in accounting" in text

    # Formatted totals (thousands separators, two decimals).
    assert "10,866.37" in text  # total operating revenue
    assert "10,395.00" in text  # Rooms subtotal
    assert "410.00" in text  # misc income
    # Misc income lines render above their total, like taxes/settlements.
    assert "Parking" in text
    assert "Total Miscellaneous Income" in text
    assert "1,573.29" in text  # taxes total
    assert "-7,093.48" in text  # settlements total

    # Segment split shows % of rooms revenue: 8316/10395 = 80.0%.
    assert "80.0%" in text
    assert "20.0%" in text

    # Empty "other" section is omitted entirely.
    assert "OTHER" not in text


def test_text_range_mode_header() -> None:
    text = render_sos_text(build_sos_range())
    assert "Period: 2026-07-01 .. 2026-07-31" in text
    assert "Business date:" not in text


def test_text_prior_year_columns_toggle() -> None:
    with_prior = render_sos_text(build_sos())
    assert "DAY PY" in with_prior
    assert "YTD PY" in with_prior
    assert "83.3" in with_prior
    assert "74.0" in with_prior

    no_prior = render_sos_text(replace(build_sos(), statistics=_STATS_NO_PRIOR))
    assert "DAY" in no_prior
    assert "PY" not in no_prior


def test_text_other_section_when_nonempty() -> None:
    sos = replace(
        build_sos(),
        other=[SosLine("Deposits", "Deposits", "Advance Deposit", Decimal("250.00"))],
        other_total=Decimal("250.00"),
    )
    text = render_sos_text(sos)
    assert "OTHER" in text
    assert "Advance Deposit" in text
    assert "Total Other" in text
    assert "250.00" in text


def test_text_percent_of_zero_rooms_revenue() -> None:
    sos = replace(
        build_sos(),
        rooms_segments=[SegmentLine("COMP", Decimal("3"), Decimal("0.00"))],
    )
    text = render_sos_text(sos)
    assert "COMP" in text
    assert "n/a" in text


def test_text_long_label_keeps_separator_space() -> None:
    long_item = "X" * 80  # wider than the label column
    sos = replace(
        build_sos(),
        other=[SosLine("Deposits", "Deposits", long_item, Decimal("250.00"))],
        other_total=Decimal("250.00"),
    )
    text = render_sos_text(sos)
    assert f"{long_item} " in text  # label never fuses into the amount


# --- json -------------------------------------------------------------------


def test_json_round_trip_exact_decimals() -> None:
    data = json.loads(render_sos_json(build_sos()))

    assert data["property_id"] == "HISJ"
    assert data["pms_source"] == "OPERA"
    assert data["business_date"] == "2026-07-07"
    assert data["date_from"] is None
    assert data["date_to"] is None

    # Decimals serialize as exact strings, never floats.
    assert data["total_operating_revenue"] == "10866.37"
    assert data["misc_income"] == [
        {
            "major": "Miscellaneous Income",
            "sub_category": "Parking",
            "line_item": "Parking",
            "total": "410.00",
        }
    ]
    assert data["misc_income_total"] == "410.00"
    assert data["taxes_total"] == "1573.29"
    assert data["settlements_total"] == "-7093.48"

    rooms = data["operated_departments"][0]
    assert rooms["sub_category"] == "Rooms"
    assert rooms["total"] == "10395.00"
    assert rooms["lines"][1] == {
        "major": "Rooms Revenue",
        "sub_category": "Rooms",
        "line_item": "Transient Rooms Revenue",
        "total": "9000.00",
    }

    assert data["rooms_segments"][1] == {
        "segment": "TRANSIENT",
        "rooms": "40",
        "room_revenue": "8316.00",
    }

    adr = data["statistics"][0]
    assert adr["metric_code"] == "ADR"
    assert adr["day"] == "207.88"
    assert adr["mtd_prior"] is None
    assert adr["ytd_prior"] == "190.00"

    assert data["other"] == []
    assert data["other_total"] == "0.00"


def test_json_range_mode() -> None:
    data = json.loads(render_sos_json(build_sos_range()))
    assert data["business_date"] is None
    assert data["date_from"] == "2026-07-01"
    assert data["date_to"] == "2026-07-31"


# --- csv --------------------------------------------------------------------


def test_csv_rows_and_spot_values() -> None:
    out = render_sos_csv(build_sos())
    rows = list(csv.reader(io.StringIO(out)))

    assert rows[0] == ["section", "sub", "line_item", "period", "value"]

    # 1 header + 3 meta + 3 operated lines + 2 dept subtotals
    # + 1 misc line + 1 misc total + 1 TOR
    # + 2 taxes + 1 taxes_total + 1 settlement + 1 settlements_total
    # + 0 other + 1 other_total + 4 segment rows (2 segments x rooms/revenue)
    # + 11 statistic cells (ADR has 5 non-None values, OCC_PCT has 6).
    assert len(rows) == 33

    assert ["total_operating_revenue", "", "", "TOTAL", "10866.37"] in rows
    assert ["misc_income", "Parking", "Parking", "TOTAL", "410.00"] in rows
    assert ["misc_income_total", "", "", "TOTAL", "410.00"] in rows
    assert [
        "operated_departments",
        "Rooms",
        "Transient Rooms Revenue",
        "TOTAL",
        "9000.00",
    ] in rows
    assert ["operated_departments_total", "Rooms", "", "TOTAL", "10395.00"] in rows
    assert ["other_total", "", "", "TOTAL", "0.00"] in rows
    assert ["rooms_segments", "TRANSIENT", "room_revenue", "TOTAL", "8316.00"] in rows
    assert ["statistics", "OCC_PCT", "", "MTD_PRIOR", "78.0"] in rows
    # None statistic cells are skipped, not emitted as empty strings.
    assert ["statistics", "ADR", "", "MTD_PRIOR", ""] not in rows

    assert ["meta", "business_date", "", "", "2026-07-07"] in rows

    # Every field is quoted.
    assert '"total_operating_revenue"' in out


def test_csv_range_mode_meta() -> None:
    rows = list(csv.reader(io.StringIO(render_sos_csv(build_sos_range()))))
    assert ["meta", "date_from", "", "", "2026-07-01"] in rows
    assert ["meta", "date_to", "", "", "2026-07-31"] in rows
    assert not any(r[1] == "business_date" for r in rows if r[0] == "meta")
