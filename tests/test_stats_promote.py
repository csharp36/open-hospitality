import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

import pytest

from usali.adaptors import skytouch_hotel_statistics
from usali.adaptors.pdf import Word
from usali.models import UsaliStatisticFact
from usali.schemas import StatisticRecord
from usali.stats_promote import _CANONICAL_PERIODS, promote_statistics
from usali.stats_stage import stage_statistics


@pytest.fixture(autouse=True)
def _founding_org(founding_org):
    """L1: every test here writes tenant rows; the org-1 FK target must exist
    (see conftest.founding_org)."""


def _rec(label: str, period: str, value: str, prior: bool = False) -> StatisticRecord:
    return StatisticRecord(
        property_id="HISJ", pms_source="OPERA", report_type="manager_flash",
        business_date=date(2026, 7, 7), metric_label=label, period_label=period,
        is_prior_year=prior, value=Decimal(value),
    )


def test_promotes_curated_metrics_and_canonical_periods(db_session):
    stage_statistics(
        db_session,
        [
            _rec("ADR", "DAY", "167.66"),
            _rec("ADR", "MONTH", "154.06"),          # MONTH -> MTD
            _rec("ADR", "YEAR", "162.43", prior=True),  # YEAR -> YTD, prior kept
            _rec("Clean Rooms", "DAY", "0"),         # uncurated -> not promoted
        ],
        source_file="mf.pdf", file_hash="p1",
    )
    db_session.commit()
    result = promote_statistics(
        db_session, "mapping/statistics.yaml", source="OPERA", business_date=date(2026, 7, 7)
    )
    db_session.commit()
    assert result.promoted == 3
    assert result.ignored == 1
    facts = db_session.execute(select(UsaliStatisticFact)).scalars().all()
    by_key = {(f.metric_code, f.period, f.is_prior_year): f.value for f in facts}
    assert by_key[("ADR", "DAY", False)] == Decimal("167.66")
    assert by_key[("ADR", "MTD", False)] == Decimal("154.06")
    assert by_key[("ADR", "YTD", True)] == Decimal("162.43")


def test_promotion_is_idempotent(db_session):
    stage_statistics(
        db_session, [_rec("ADR", "DAY", "167.66")], source_file="mf.pdf", file_hash="p2"
    )
    db_session.commit()
    promote_statistics(db_session, "mapping/statistics.yaml", source="OPERA",
                       business_date=date(2026, 7, 7))
    db_session.commit()
    second = promote_statistics(db_session, "mapping/statistics.yaml", source="OPERA",
                                business_date=date(2026, 7, 7))
    db_session.commit()
    assert second.promoted == 0 and second.skipped == 1
    assert len(db_session.execute(select(UsaliStatisticFact)).scalars().all()) == 1


def _skytouch_fixture_records() -> list[StatisticRecord]:
    # The committed words of the real Standard Audit Pack section: five curated
    # metrics x five period columns = 25 records, all with pms_source SKYTOUCH.
    words = [
        Word(text=x["text"], x0=x["x0"], top=x["top"])
        for x in json.loads(
            Path("tests/fixtures/skytouch_hotel_statistics_words.json").read_text()
        )
    ]
    return skytouch_hotel_statistics.parse_hotel_statistics(
        words, property_id="STDEMO", business_date=date(2026, 6, 21)
    )


def test_skytouch_day_and_mtd_facts_promote(db_session):
    recs = _skytouch_fixture_records()
    assert len(recs) == 25 and {r.period_label for r in recs} == {
        "ACTUAL", "PTD", "LY_PTD", "YTD", "LY_YTD"
    }
    stage_statistics(db_session, recs, source_file="pack.pdf", file_hash="st1")
    db_session.commit()
    result = promote_statistics(
        db_session, "mapping/statistics.yaml", source="SKYTOUCH",
        business_date=date(2026, 6, 21),
    )
    db_session.commit()
    # Every one of the 25 rows is a curated metric in a canonical period.
    assert (result.promoted, result.ignored, result.skipped) == (25, 0, 0)
    facts = db_session.execute(select(UsaliStatisticFact)).scalars().all()
    by_key = {(f.metric_code, f.period, f.is_prior_year): f.value for f in facts}
    assert by_key[("ROOMS_OCCUPIED", "DAY", False)] == Decimal("62")      # ACTUAL
    assert by_key[("ADR", "MTD", False)] == Decimal("90.10")              # PTD
    assert by_key[("ADR", "MTD", True)] == Decimal("92.25")               # LY_PTD
    assert by_key[("ROOM_REVENUE", "YTD", True)] == Decimal("435800.00")  # LY_YTD
    assert {f.period for f in facts} == {"DAY", "MTD", "YTD"}


def test_every_skytouch_period_label_is_canonical():
    # The adapter's labels and the promote map live in different modules; this
    # is the pin that fails if either side changes alone.
    missing = set(skytouch_hotel_statistics._PERIODS) - set(_CANONICAL_PERIODS)
    assert not missing, f"SkyTouch period labels with no canonical period: {missing}"
