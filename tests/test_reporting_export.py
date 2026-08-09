"""Flat fact-table exports for ERP/BI (P6 Task 4) against real seeded data.

Seeds the six sample PDFs via the shared `seed_six_pdfs` fixture, then exports each
fact table for the seeded business date and asserts row counts, stable stringified
columns, and the csv/json renderers (including the empty-export edge)."""

import csv
import io
import json
from datetime import date

import pytest

from usali.render import rows_to_csv, rows_to_json
from usali.reporting import export_columns, export_rows

_DAY = date(2026, 7, 7)


def test_export_rows_counts_and_shape(db_session, seed_six_pdfs):
    financial = export_rows(db_session, "financial", date_from=_DAY, date_to=_DAY)
    statistics = export_rows(db_session, "statistics", date_from=_DAY, date_to=_DAY)
    segments = export_rows(db_session, "segments", date_from=_DAY, date_to=_DAY)

    assert len(financial) == 46
    assert len(statistics) == 103
    assert len(segments) == 18

    # Every row is a flat dict of plain strings with identical column order.
    for rows in (financial, statistics, segments):
        keys = list(rows[0])
        for row in rows:
            assert list(row) == keys
            assert all(isinstance(v, str) for v in row.values())

    # Spot-check one financial row end to end: the Opera Parking fact (Sch 4,
    # 410.00 that day). Decimals stay plain strings, dates ISO, NULLs empty.
    parking = [
        r
        for r in financial
        if r["usali_line_item"] == "Parking" and r["property_id"] == "HISJ"
    ]
    assert len(parking) == 1
    row = parking[0]
    assert row["pms_source"] == "OPERA"
    assert row["business_date"] == "2026-07-07"
    assert row["usali_schedule_id"] == "4"
    assert row["amount"] == "410.0000"
    assert row["gl_account_code"] == "4200"  # misc-income GL account (P8 dictionary)


def test_export_rows_property_filter(db_session, seed_six_pdfs):
    all_rows = export_rows(db_session, "financial", date_from=_DAY, date_to=_DAY)
    hisj = export_rows(
        db_session, "financial", date_from=_DAY, date_to=_DAY, property_id="HISJ"
    )
    assert 0 < len(hisj) < len(all_rows)
    assert all(r["property_id"] == "HISJ" for r in hisj)


def test_export_rows_validation(db_session):
    with pytest.raises(ValueError, match="unknown export table"):
        export_rows(db_session, "expenses", date_from=_DAY, date_to=_DAY)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="after date_to"):
        export_rows(
            db_session, "financial", date_from=date(2026, 7, 31), date_to=date(2026, 7, 1)
        )


def test_export_renderers(db_session, seed_six_pdfs):
    segments = export_rows(db_session, "segments", date_from=_DAY, date_to=_DAY)

    csv_text = rows_to_csv(segments)
    parsed = list(csv.reader(io.StringIO(csv_text)))
    assert parsed[0] == list(segments[0])  # header carries the column order
    assert parsed[0] == export_columns("segments")  # and matches the header contract
    assert len(parsed) == len(segments) + 1
    assert parsed[1] == list(segments[0].values())

    json_rows = json.loads(rows_to_json(segments))
    assert json_rows == segments  # round-trips exactly: all values are strings


def test_export_empty_range(db_session, seed_six_pdfs):
    rows = export_rows(
        db_session, "financial", date_from=date(2020, 1, 1), date_to=date(2020, 1, 2)
    )
    assert rows == []
    # With explicit fieldnames (the CLI path) an empty export is a header-only csv.
    header_csv = rows_to_csv(rows, fieldnames=export_columns("financial"))
    assert list(csv.reader(io.StringIO(header_csv))) == [export_columns("financial")]
    # Bare renderers have no schema to derive a header from: empty csv, empty array.
    assert rows_to_csv(rows) == ""
    assert json.loads(rows_to_json(rows)) == []
