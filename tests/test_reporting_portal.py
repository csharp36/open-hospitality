"""Portal queries (P7 Task 1) against real seeded data.

Each test re-ingests the six sample PDFs through `process_file` via the shared
`seed_six_pdfs` fixture (per-test TRUNCATE keeps isolation), then asserts on the
drill-through (`line_transactions`) and property listing (`list_properties`)
queries the portal API will serve. One consolidated test function per concern.
"""

from datetime import date
from decimal import Decimal

import pytest

from usali.reporting import line_transactions, list_properties, summary_operating_statement


def test_line_transactions_parking(db_session, seed_six_pdfs):
    txns = line_transactions(
        db_session, property_id="HISJ", major="Miscellaneous Income",
        sub_category="Parking", line_item="Parking",
        date_from=date(2026, 7, 7), date_to=date(2026, 7, 7),
    )
    assert txns, "drill-through must find the staged rows behind the Parking line"
    assert {t.pms_trx_code for t in txns} == {"5105"}
    assert sum((t.amount for t in txns), Decimal("0")) == Decimal("410.00")
    t = txns[0]
    assert t.pms_source == "OPERA" and t.business_date == date(2026, 7, 7)
    assert t.pms_trx_desc  # description carried through from the stage row
    assert t.source_file.endswith(".pdf")


def test_line_transactions_reconcile_every_sos_line(db_session, seed_six_pdfs):
    # THE payoff invariant: for each financial line on each property's SOS, the
    # drill-through transactions sum exactly to the line total.
    for property_id in ("HISJ", "SSSJ"):
        sos = summary_operating_statement(
            db_session, property_id=property_id, business_date=date(2026, 7, 7)
        )
        operated = [line for dept in sos.operated_departments for line in dept.lines]
        for line in operated + sos.misc_income + sos.taxes + sos.settlements + sos.other:
            txns = line_transactions(
                db_session, property_id=property_id, major=line.major,
                sub_category=line.sub_category, line_item=line.line_item,
                date_from=date(2026, 7, 7), date_to=date(2026, 7, 7),
            )
            assert sum((t.amount for t in txns), Decimal("0")) == line.total


def test_line_transactions_ordering_and_types(db_session, seed_six_pdfs):
    sos = summary_operating_statement(
        db_session, property_id="HISJ", business_date=date(2026, 7, 7)
    )
    rooms = next(s for s in sos.operated_departments if s.sub_category == "Rooms")
    line = rooms.lines[0]
    txns = line_transactions(
        db_session, property_id="HISJ", major=line.major,
        sub_category=line.sub_category, line_item=line.line_item,
        date_from=date(2026, 7, 1), date_to=date(2026, 7, 31),
    )
    assert txns
    # Deterministic ordering: (business_date, stage_id).
    assert [(t.business_date, t.stage_id) for t in txns] == sorted(
        (t.business_date, t.stage_id) for t in txns
    )
    # Amounts come from the fact rows as exact Decimals, never floats.
    assert all(isinstance(t.amount, Decimal) for t in txns)


def test_list_properties(db_session, seed_six_pdfs):
    props = list_properties(db_session)
    assert {(p.property_id, p.pms_source) for p in props} == {
        ("HISJ", "OPERA"),
        ("SSSJ", "AUTOCLERK"),
    }
    # Ordered by property_id.
    assert [p.property_id for p in props] == sorted(p.property_id for p in props)
    hisj = next(p for p in props if p.property_id == "HISJ")
    assert hisj.first_date == hisj.last_date == date(2026, 7, 7)


def test_line_transactions_empty(db_session, seed_six_pdfs):
    # Drill-through of an unknown line is just empty — no ValueError.
    assert line_transactions(
        db_session, property_id="HISJ", major="Nope", sub_category="X",
        line_item="Y", date_from=date(2026, 7, 7), date_to=date(2026, 7, 7),
    ) == []


def test_line_transactions_rejects_inverted_range(db_session):
    with pytest.raises(ValueError, match="after date_to"):
        line_transactions(
            db_session, property_id="HISJ", major="Miscellaneous Income",
            sub_category="Parking", line_item="Parking",
            date_from=date(2026, 7, 31), date_to=date(2026, 7, 1),
        )


def test_list_properties_empty(db_session):
    assert list_properties(db_session) == []
