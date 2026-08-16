"""F2 (issue #9): the demo world exposes the performance metrics.

Two seams, one per demo path:

* `_seed_property_config` writes the per-property `PropertyStatConfig`
  beside the #8 property config — HISJ nets comp/house-use out of ADR's
  denominator (`exclude_comp_house`), SSSJ takes rooms as reported
  (`as_reported`) — so `_adr_room_basis` (and every ADR surface that reads
  it) has a basis. It runs on every seed (outside the `_seed_world`
  sentinel) and is idempotent, so a re-deploy backfills it.

* `_seed_synthetic_year` stages/promotes statistics DIRECTLY, bypassing
  `process_file`'s automatic `record_coverage`; without the coverage row
  it writes itself, `complete_days` — and therefore every trend and
  comparison — would ignore the seeded days. The pin runs a two-day
  slice and asserts the days land as complete and `core_metrics` reads
  them.
"""

import importlib.util
from datetime import date
from pathlib import Path

from sqlalchemy import delete, func, select

from usali.models import (
    FiscalCalendar,
    IngestionCoverage,
    OutOfOrderRoom,
    PropertyStatConfig,
    RoomInventory,
)
from usali.performance import complete_days, core_metrics
from usali.property_config_api import _adr_room_basis

_SCRIPT = Path(__file__).parent.parent / "scripts" / "demo_seed.py"
_spec = importlib.util.spec_from_file_location("demo_seed_perf", _SCRIPT)
assert _spec is not None and _spec.loader is not None
demo_seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo_seed)


def test_seed_property_config_writes_per_property_adr_basis(db_session):
    """`_seed_property_config` lands one stat-config row per property, and the
    reader every ADR surface consults returns each property's basis. This
    config lives on the always-run path (NOT the sentinel-sealed
    `_seed_world`), so a re-deploy can backfill it onto an already-seeded
    world — the live demo lost room inventory when it was still sealed."""
    demo_seed._seed_base(db_session)  # properties (the config's FK target)
    demo_seed._seed_property_config(db_session)

    rows = {r.property_id: r.adr_room_basis for r in db_session.execute(
        select(PropertyStatConfig)).scalars()}
    assert rows == {"HISJ": "exclude_comp_house", "SSSJ": "as_reported"}
    assert _adr_room_basis(db_session, "HISJ") == "exclude_comp_house"
    assert _adr_room_basis(db_session, "SSSJ") == "as_reported"


def test_seed_property_config_is_idempotent(db_session):
    """Running the config seed twice is a no-op — the always-run path must not
    stack duplicate inventory / OOO rows on every deploy. Room inventory and
    OOO have no upsert on insert, so a second run must find-or-create them."""
    demo_seed._seed_base(db_session)
    demo_seed._seed_property_config(db_session)
    demo_seed._seed_property_config(db_session)

    def _count(model):
        return db_session.scalar(select(func.count()).select_from(model))

    assert _count(RoomInventory) == 3  # HISJ x2, SSSJ x1
    assert _count(FiscalCalendar) == 2
    assert _count(PropertyStatConfig) == 2
    assert _count(OutOfOrderRoom) == 1


def test_synthetic_year_records_coverage_for_complete_days(
    db_session, monkeypatch
):
    """The direct stage/promote path records its own coverage, so the seeded
    days count as complete and `core_metrics` reads them — the trend/
    comparison surfaces depend on exactly this."""
    import usali.synthetic_year as sy

    days = [date(2026, 6, 1), date(2026, 6, 2)]
    monkeypatch.setattr(sy, "synthetic_dates", lambda: days)
    demo_seed._seed_base(db_session)  # properties, mappings, schedules
    # In-force inventory (the #8 seeding the live demo also writes); without
    # it complete_days rejects every day regardless of coverage.
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2025, 1, 1),
                      total_rooms=140),
        RoomInventory(property_id="SSSJ", effective_date=date(2025, 1, 1),
                      total_rooms=90),
    ])
    db_session.commit()

    demo_seed._seed_synthetic_year(db_session)

    # SSSJ speaks AUTOCLERK -> manager_report; the coverage row is what the
    # synthetic path adds (process_file is never called on this path).
    landed = {(c.business_date, c.report_type) for c in db_session.execute(
        select(IngestionCoverage)
        .where(IngestionCoverage.property_id == "SSSJ")).scalars()}
    assert landed == {(d, "manager_report") for d in days}
    assert {c.report_type for c in db_session.execute(
        select(IngestionCoverage)
        .where(IngestionCoverage.property_id == "HISJ")).scalars()} == {
        "manager_flash"}

    assert complete_days(db_session, "SSSJ", days[0], days[1]) == set(days)
    metrics = core_metrics(db_session, "SSSJ", days[0], days[0],
                           basis="as_reported")
    assert metrics.rooms_available > 0
    assert metrics.rooms_sold > 0
    assert metrics.adr is not None


def test_synthetic_year_backfills_coverage_for_pre_existing_days(
    db_session, monkeypatch
):
    """A world first seeded before coverage recording existed has transformed
    facts but no coverage rows — every day reads 'incomplete'. Re-running the
    synthetic seed must BACKFILL coverage for those already-present days (the
    per-day loop only covers newly-seeded days), restoring complete_days."""
    import usali.synthetic_year as sy

    days = [date(2026, 6, 1), date(2026, 6, 2)]
    monkeypatch.setattr(sy, "synthetic_dates", lambda: days)
    demo_seed._seed_base(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2025, 1, 1),
                      total_rooms=140),
        RoomInventory(property_id="SSSJ", effective_date=date(2025, 1, 1),
                      total_rooms=90),
    ])
    db_session.commit()
    demo_seed._seed_synthetic_year(db_session)  # facts + coverage

    # Simulate the pre-#9 world: facts stay transformed, coverage rows gone.
    db_session.execute(delete(IngestionCoverage))
    db_session.commit()
    assert db_session.scalar(
        select(func.count()).select_from(IngestionCoverage)) == 0
    assert complete_days(db_session, "SSSJ", days[0], days[1]) == set()  # excluded

    # Re-run: the days are already transformed, so ONLY the backfill can repair.
    demo_seed._seed_synthetic_year(db_session)

    landed = {(c.property_id, c.business_date, c.report_type) for c in
              db_session.execute(select(IngestionCoverage)).scalars()}
    assert landed == (
        {("HISJ", d, "manager_flash") for d in days}
        | {("SSSJ", d, "manager_report") for d in days}
    )
    assert complete_days(db_session, "HISJ", days[0], days[1]) == set(days)
    assert complete_days(db_session, "SSSJ", days[0], days[1]) == set(days)
