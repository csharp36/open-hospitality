"""SkyTouch PMS mapping dictionary (S9).

SkyTouch transaction codes are franchise-configurable, so the curated YAML is
seeded LOW/needs-review across the board. These tests pin the two things a
reviewer relies on before touching the codes: the file parses into well-formed
rows, and it lands in the dictionary table under source SKYTOUCH.
"""

from pathlib import Path

import yaml
from sqlalchemy import select

from usali.mapping.loader import load_mappings
from usali.models import UsaliMappingDictionary

SKYTOUCH_YAML = "mapping/skytouch.yaml"


def _rows() -> list[dict]:
    return yaml.safe_load(Path(SKYTOUCH_YAML).read_text())


def test_every_row_is_skytouch_and_needs_review():
    rows = _rows()
    assert rows, "skytouch.yaml is empty"
    for row in rows:
        assert row["source"] == "SKYTOUCH", row["code"]
        assert row["review_status"] == "needs-review", row["code"]
        assert row["confidence"] == "LOW", row["code"]
        # schedule_id is a P&L schedule number or null for non-P&L codes.
        assert row["schedule_id"] is None or isinstance(row["schedule_id"], int), row["code"]


def test_room_charge_and_state_tax_classifications():
    by_code = {row["code"]: row for row in _rows()}

    room = by_code["RM"]
    assert room["schedule_id"] == 1
    assert room["sub"] == "Rooms"

    # T1 is a pass-through tax: non-P&L, so no schedule.
    assert by_code["T1"]["schedule_id"] is None


def test_load_mappings_lands_skytouch_room_row(db_session):
    load_mappings(db_session, SKYTOUCH_YAML)
    db_session.commit()

    row = db_session.execute(
        select(UsaliMappingDictionary).where(
            UsaliMappingDictionary.pms_source == "SKYTOUCH",
            UsaliMappingDictionary.pms_trx_code == "RM",
        )
    ).scalars().one()
    assert row.usali_schedule_id == 1
    assert row.usali_sub_category == "Rooms"
    assert row.usali_line_item == "Room Revenue"
    assert row.review_status == "needs-review"
