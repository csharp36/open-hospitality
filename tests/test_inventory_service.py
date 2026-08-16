from datetime import date

import pytest

from usali.inventory import (
    InventoryInconsistent,
    InventoryNotConfigured,
    rooms_available,
    total_rooms_on,
)
from usali.models import Organization, OutOfOrderRoom, Property, RoomInventory


def _prop(session, pid="HISJ"):
    # Property.org_id carries a real FK onto organization (l1a0orgid) — org 1
    # must exist before a property can reference it. `merge` so a second call
    # in the same test (a different property_id, same org) is a no-op re-fetch
    # rather than a duplicate-PK insert.
    session.merge(Organization(org_id=1, name="Org"))
    session.add(Property(property_id=pid, org_id=1, name=pid, pms_source="OPERA"))
    session.flush()


def test_total_rooms_on_returns_in_force_count(db_session):
    _prop(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140),
        RoomInventory(property_id="HISJ", effective_date=date(2026, 6, 1), total_rooms=138),
    ])
    db_session.commit()
    assert total_rooms_on(db_session, "HISJ", date(2026, 3, 15)) == 140  # between records
    assert total_rooms_on(db_session, "HISJ", date(2026, 6, 1)) == 138   # on the change day
    assert total_rooms_on(db_session, "HISJ", date(2026, 9, 1)) == 138   # after


def test_total_rooms_on_refuses_before_first_record(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    with pytest.raises(InventoryNotConfigured):
        total_rooms_on(db_session, "HISJ", date(2025, 12, 31))


def test_rooms_available_simple(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    # Jan 2026: 31 days × 140 = 4340, no OOO
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 4340


def test_rooms_available_subtracts_ooo_room_nights(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    # 3 rooms out for 7 days (Jan 10..16 inclusive) = 21 room-nights
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 10),
                                  end_date=date(2026, 1, 16), room_count=3, reason_code="renovation"))
    db_session.commit()
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 4340 - 21


def test_rooms_available_clamps_partial_overlap(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    # block Jan 28..Feb 4; window ends Jan 31 -> only Jan 28,29,30,31 = 4 nights count
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 28),
                                  end_date=date(2026, 2, 4), room_count=2, reason_code="maintenance"))
    db_session.commit()
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 4340 - 8


def test_rooms_available_handles_mid_window_inventory_change(db_session):
    _prop(db_session)
    db_session.add_all([
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140),
        RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 15), total_rooms=138),
    ])
    db_session.commit()
    # Jan 1..14 = 14 days × 140 = 1960; Jan 15..31 = 17 days × 138 = 2346
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 31)) == 1960 + 2346


def test_rooms_available_counts_leap_day(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2024, 1, 1), total_rooms=100))
    db_session.commit()
    # Feb 2024 has 29 days
    assert rooms_available(db_session, "HISJ", date(2024, 2, 1), date(2024, 2, 29)) == 2900


def test_rooms_available_refuses_window_before_inventory(db_session):
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.commit()
    with pytest.raises(InventoryNotConfigured):
        rooms_available(db_session, "HISJ", date(2025, 12, 25), date(2026, 1, 5))


def test_rooms_available_refuses_when_one_block_exceeds_inventory(db_session):
    # A single OOO block bigger than the day's inventory would make the day's
    # availability negative — refuse loudly rather than return a negative
    # denominator for #9 (adr-010).
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=10))
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 1),
                                  end_date=date(2026, 1, 1), room_count=25, reason_code="damage"))
    db_session.commit()
    with pytest.raises(InventoryInconsistent):
        rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1))


def test_rooms_available_refuses_when_overlapping_blocks_exceed_inventory(db_session):
    # Two blocks each individually <= inventory but together exceeding it: there
    # is no overlap/uniqueness constraint on OOO blocks, so the per-day sum must
    # still catch the negative rather than double-subtract into a negative total.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=10))
    db_session.add_all([
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
                       room_count=7, reason_code="maintenance"),
        OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
                       room_count=7, reason_code="deep_clean"),
    ])
    db_session.commit()
    with pytest.raises(InventoryInconsistent):
        rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 1))


def test_rooms_available_allows_a_fully_out_of_service_day(db_session):
    # A day with EVERY room out of service is a legitimate 0 (not negative), and
    # the surrounding days are unaffected — floor at 0, never refuse the zero.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=10))
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 2),
                                  end_date=date(2026, 1, 2), room_count=10, reason_code="renovation"))
    db_session.commit()
    # Jan 1: 10, Jan 2: 0, Jan 3: 10 -> 20
    assert rooms_available(db_session, "HISJ", date(2026, 1, 1), date(2026, 1, 3)) == 20


def test_rooms_available_counts_block_touching_window_start(db_session):
    # Block ends ON the window's first day: exactly one room-night deducted.
    # Pins the inclusive `end_date >= start` SQL prefilter edge.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 5),
                                  end_date=date(2026, 1, 10), room_count=2, reason_code="maintenance"))
    db_session.commit()
    # window Jan 10..Jan 20 = 11 days x 140 = 1540; block overlaps only Jan 10 -> -2
    assert rooms_available(db_session, "HISJ", date(2026, 1, 10), date(2026, 1, 20)) == 1540 - 2


def test_rooms_available_counts_block_touching_window_end(db_session):
    # Block starts ON the window's last day: exactly one room-night deducted.
    # Pins the inclusive `start_date <= end` SQL prefilter edge.
    _prop(db_session)
    db_session.add(RoomInventory(property_id="HISJ", effective_date=date(2026, 1, 1), total_rooms=140))
    db_session.add(OutOfOrderRoom(property_id="HISJ", start_date=date(2026, 1, 20),
                                  end_date=date(2026, 1, 25), room_count=2, reason_code="maintenance"))
    db_session.commit()
    # window Jan 10..Jan 20 = 11 days x 140 = 1540; block overlaps only Jan 20 -> -2
    assert rooms_available(db_session, "HISJ", date(2026, 1, 10), date(2026, 1, 20)) == 1540 - 2
