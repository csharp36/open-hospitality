"""A non-SOS-backing source (design D-OH22.6) gets a statistics-plus-notice
statement, appears in the property picker, and never reaches the journal path."""

import json
import shutil
from dataclasses import fields, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.grants import grant_role
from tests.test_portal_api import _make_client
from usali import reporting
from usali.detect import SOURCE_NOTICES
from usali.ingestion import process_file
from usali.mapping.loader import load_mappings
from usali.mapping.property_registry import seed_properties
from usali.mapping.schedules import seed_schedules
from usali.render import render_sos_csv, render_sos_json, render_sos_text

PDF = Path("docs/reference/samples/HotelKey - Hotel Statistics (mock).pdf")
BD = date(2026, 8, 13)


@pytest.fixture
def hotelkey_day(db_session, founding_org, tmp_path):
    seed_schedules(db_session, "mapping/usali_schedules.yaml")
    load_mappings(db_session, "mapping/hotelkey.yaml")
    seed_properties(db_session, "mapping/properties.yaml")
    db_session.commit()
    drop = tmp_path / PDF.name
    shutil.copy(PDF, drop)
    process_file(db_session, drop, processed_dir=tmp_path / "done", failed_dir=tmp_path / "fail")
    return db_session


def test_statistics_only_statement_carries_the_notice_and_the_statistics(hotelkey_day):
    report = reporting.statistics_only_statement(
        hotelkey_day,
        property_id="HKDEMO",
        pms_source="HOTELKEY",
        notice=SOURCE_NOTICES["HOTELKEY"],
        business_date=BD,
    )
    assert report.source_notice == SOURCE_NOTICES["HOTELKEY"]
    assert report.pms_source == "HOTELKEY" and report.business_date == BD
    assert report.operated_departments == [] and report.taxes == [] and report.settlements == []
    assert report.total_operating_revenue == Decimal("0")
    codes = {row.metric_code: row for row in report.statistics}
    assert codes["OCCUPANCY_PCT"].day == Decimal("50.00")
    assert codes["ADR"].mtd_prior == Decimal("124.00")


def test_statistics_only_statement_with_no_statistics_is_no_facts(hotelkey_day):
    with pytest.raises(reporting.NoFactsError):
        reporting.statistics_only_statement(
            hotelkey_day,
            property_id="HKDEMO",
            pms_source="HOTELKEY",
            notice="n",
            business_date=date(1999, 1, 1),
        )


def test_the_journal_statement_still_refuses_a_statistics_only_property(hotelkey_day):
    with pytest.raises(reporting.NoFactsError):
        reporting.summary_operating_statement_from_journal(
            hotelkey_day, property_id="HKDEMO", business_date=BD
        )


def test_a_statistics_only_property_is_in_the_property_list(hotelkey_day):
    props = {p.property_id: p for p in reporting.list_properties(hotelkey_day)}
    assert props["HKDEMO"].pms_source == "HOTELKEY"
    assert (props["HKDEMO"].first_date, props["HKDEMO"].last_date) == (BD, BD)


def test_renderers_print_the_notice(hotelkey_day):
    report = reporting.statistics_only_statement(
        hotelkey_day,
        property_id="HKDEMO",
        pms_source="HOTELKEY",
        notice="NOTICE TEXT",
        business_date=BD,
    )
    text = render_sos_text(report)
    assert "NOTICE TEXT" in text
    # The notice says no revenue section is shown; a printed 0.00 would
    # contradict it.
    assert "TOTAL OPERATING REVENUE" not in text
    assert "OPERATED DEPARTMENTS" not in text
    assert "MISCELLANEOUS INCOME" not in text
    assert "STATISTICS" in text
    assert json.loads(render_sos_json(report))["source_notice"] == "NOTICE TEXT"
    assert '"meta","source_notice","","","NOTICE TEXT"' in render_sos_csv(report)


def test_a_journal_backed_statement_renders_no_notice_line(hotelkey_day):
    # A None notice must leave every renderer's output free of it.
    built = reporting.statistics_only_statement(
        hotelkey_day, property_id="HKDEMO", pms_source="HOTELKEY", notice="x", business_date=BD
    )
    report = replace(built, source_notice=None)
    text = render_sos_text(report)
    assert "NOTE:" not in text
    assert "TOTAL OPERATING REVENUE" in text
    assert json.loads(render_sos_json(report))["source_notice"] is None
    assert "source_notice" not in render_sos_csv(report)


def test_sos_api_returns_the_notice_for_a_hotelkey_property(hotelkey_day, db_engine, tmp_path):
    # tests/test_portal_api.py::_make_client builds a TestClient over `create_app`
    # with an accountant bearer token. As in that file's `client` fixture, the
    # minted accountant's authority is its org-wide DB grant, committed because
    # the endpoint reads through its own session factory, not db_session.
    grant_role(hotelkey_day, "accountant")
    hotelkey_day.commit()
    client: TestClient = _make_client(db_engine, tmp_path)
    r = client.get("/api/sos", params={"property": "HKDEMO", "date": BD.isoformat()})
    assert r.status_code == 200
    body = r.json()
    assert body["source_notice"] == SOURCE_NOTICES["HOTELKEY"]
    assert body["pms_source"] == "HOTELKEY"
    assert body["operated_departments"] == [] and body["statistics"]
    r2 = client.get("/api/properties")
    assert any(p["property_id"] == "HKDEMO" and p["pms_source"] == "HOTELKEY" for p in r2.json())


def test_sos_api_still_404s_a_hotelkey_property_with_no_statistics_that_day(
    hotelkey_day, db_engine, tmp_path
):
    # The notice branch must not turn "nothing landed" into an empty 200.
    grant_role(hotelkey_day, "accountant")
    hotelkey_day.commit()
    client = _make_client(db_engine, tmp_path)
    r = client.get("/api/sos", params={"property": "HKDEMO", "date": "1999-01-01"})
    assert r.status_code == 404


def test_every_sos_field_is_classified_revenue_or_kept():
    # `statistics_only_statement` empties exactly _REVENUE_SIDE and fills exactly
    # _KEPT_SIDE; a new SosReport field fails here until it is classified.
    names = {f.name for f in fields(reporting.SosReport)}
    assert names == reporting._REVENUE_SIDE | reporting._KEPT_SIDE
    assert not (reporting._REVENUE_SIDE & reporting._KEPT_SIDE)
