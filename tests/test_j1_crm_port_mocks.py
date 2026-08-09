"""J1: the CRM demand port + the two mocks (plan decisions 1-2).

The port is deliberately narrow: capabilities + fetch_demand over a date
window, normalized to CrmDemandDay — each figure `int | None`, where None
means "this CRM does not speak that dimension", never zero (the D2
forecast-null rule applied to the feed).

The two mocks ARE the contract until a sandbox exists, and they are
deliberately DIFFERENT-shaped so the J3 adapters prove the abstraction:
Delphi speaks a paged, PascalCase enterprise envelope of room blocks and
rooms-on-the-books (no covers, subscription-key auth); Tripleseat speaks
snake_case events with covers and optional nested room blocks (no pace,
api-key-param auth). Both worlds are SALTED with synthetic contact and
revenue fields — pinned present here so J3's drop-unread test has teeth.
Synthetic data only.
"""

from datetime import date

import httpx
import pytest

from usali.crm_feed import (
    CrmCapabilities,
    CrmDemandDay,
    InMemoryCrmFeed,
)
from usali.delphi_mock import DELPHI_HOTEL_REF, create_mock_delphi
from usali.qbo_client import SyncASGITransport
from usali.tripleseat_mock import TRIPLESEAT_LOCATION_ID, create_mock_tripleseat

_WINDOW = {"start": "2026-08-03", "end": "2026-08-09"}


def _delphi_client():
    return httpx.Client(transport=SyncASGITransport(create_mock_delphi()),
                        base_url="http://mock-delphi")


def _tripleseat_client():
    return httpx.Client(transport=SyncASGITransport(create_mock_tripleseat()),
                        base_url="http://mock-tripleseat")


_DELPHI_AUTH = {"Ocp-Apim-Subscription-Key": "mock"}


# --- the Delphi mock ---------------------------------------------------------


def test_delphi_requires_the_subscription_key():
    with _delphi_client() as c:
        r = c.get(f"/api/v2/hotels/{DELPHI_HOTEL_REF}/roomblocks",
                  params={"startDate": "08/03/2026", "endDate": "08/09/2026"})
    assert r.status_code == 401


def test_delphi_serves_paged_room_blocks_with_salt():
    """The block world walks TWO pages (the adapter must paginate), stay
    dates carry blocked + picked-up rooms, and every block is salted with
    Contact and AverageRate — fields the J3 adapter must drop unread."""
    with _delphi_client() as c:
        one = c.get(f"/api/v2/hotels/{DELPHI_HOTEL_REF}/roomblocks",
                    params={"startDate": "08/03/2026",
                            "endDate": "08/09/2026", "page": 1},
                    headers=_DELPHI_AUTH)
        assert one.status_code == 200, one.text
        body = one.json()
        assert body["Page"] == 1 and body["TotalPages"] == 2
        two = c.get(f"/api/v2/hotels/{DELPHI_HOTEL_REF}/roomblocks",
                    params={"startDate": "08/03/2026",
                            "endDate": "08/09/2026", "page": 2},
                    headers=_DELPHI_AUTH)
        assert two.json()["Page"] == 2

    blocks = body["Items"] + two.json()["Items"]
    assert len(blocks) >= 2
    fat = next(b for b in blocks if b["BlockName"] == "Acme Corp Annual")
    thursday = next(d for d in fat["StayDates"] if d["Date"] == "08/06/2026")
    assert thursday["BlockedRooms"] == 40
    assert thursday["PickedUpRooms"] == 25
    for block in blocks:
        assert "Contact" in block and "AverageRate" in block  # the salt


def test_delphi_serves_rooms_on_the_books():
    with _delphi_client() as c:
        r = c.get(f"/api/v2/hotels/{DELPHI_HOTEL_REF}/occupancy",
                  params={"startDate": "08/03/2026", "endDate": "08/09/2026"},
                  headers=_DELPHI_AUTH)
    assert r.status_code == 200
    items = r.json()["Items"]
    by_date = {i["Date"]: i["RoomsOnTheBooks"] for i in items}
    assert by_date["08/06/2026"] > by_date["08/03/2026"]  # the fat Thursday
    assert all(v >= 0 for v in by_date.values())


def test_delphi_unknown_hotel_404s():
    with _delphi_client() as c:
        r = c.get("/api/v2/hotels/NOPE/roomblocks",
                  params={"startDate": "08/03/2026", "endDate": "08/09/2026"},
                  headers=_DELPHI_AUTH)
    assert r.status_code == 404


# --- the Tripleseat mock -----------------------------------------------------


def test_tripleseat_requires_the_api_key():
    with _tripleseat_client() as c:
        r = c.get("/v1/events.json",
                  params={"location_id": TRIPLESEAT_LOCATION_ID,
                          "start_date": _WINDOW["start"],
                          "end_date": _WINDOW["end"]})
    assert r.status_code == 401


def test_tripleseat_serves_events_with_covers_and_salt():
    """Events carry guest counts (covers) and an OPTIONAL nested room
    block; every event is salted with a contact and a grand_total — the
    J3 drop-unread targets."""
    with _tripleseat_client() as c:
        r = c.get("/v1/events.json",
                  params={"location_id": TRIPLESEAT_LOCATION_ID,
                          "api_key": "mock",
                          "start_date": _WINDOW["start"],
                          "end_date": _WINDOW["end"]})
    assert r.status_code == 200, r.text
    body = r.json()
    events = body["events"]
    assert body["total_pages"] == 1 and events
    wedding = next(e for e in events if e["name"] == "Nguyen Wedding")
    assert wedding["event_date"] == "2026-08-06"
    assert wedding["guest_count"] == 120
    assert wedding["room_block"]["rooms_per_night"] == [
        {"date": "2026-08-06", "rooms": 15},
    ]
    lunch = next(e for e in events if e["name"] == "Rotary Lunch")
    assert lunch["guest_count"] == 45 and lunch["room_block"] is None
    for event in events:
        assert "contact" in event and "grand_total" in event  # the salt


def test_tripleseat_unknown_location_404s():
    with _tripleseat_client() as c:
        r = c.get("/v1/events.json",
                  params={"location_id": 999999, "api_key": "mock",
                          "start_date": _WINDOW["start"],
                          "end_date": _WINDOW["end"]})
    assert r.status_code == 404


# --- the shapes stay deliberately different ----------------------------------


def test_the_two_wire_shapes_deliberately_differ():
    """The abstraction is proven, not assumed (the payroll-port posture):
    if the mocks converge on one shape, the adapters stop earning their
    keep. Delphi: PascalCase paged envelope, US-format dates, header
    auth. Tripleseat: snake_case, ISO dates, key-param auth."""
    with _delphi_client() as c:
        d = c.get(f"/api/v2/hotels/{DELPHI_HOTEL_REF}/roomblocks",
                  params={"startDate": "08/03/2026", "endDate": "08/09/2026"},
                  headers=_DELPHI_AUTH).json()
    with _tripleseat_client() as c:
        t = c.get("/v1/events.json",
                  params={"location_id": TRIPLESEAT_LOCATION_ID,
                          "api_key": "mock",
                          "start_date": _WINDOW["start"],
                          "end_date": _WINDOW["end"]}).json()
    assert "Items" in d and "TotalPages" in d
    assert "events" in t and "total_pages" in t
    assert "/" in d["Items"][0]["StayDates"][0]["Date"]      # 08/06/2026
    assert "-" in t["events"][0]["event_date"]               # 2026-08-06


# --- the normalized shape ----------------------------------------------------


def test_demand_day_holds_both_providers_dimensions():
    """None means "this CRM does not speak that dimension" — never 0."""
    delphi_ish = CrmDemandDay(
        stay_date=date(2026, 8, 6), rooms_on_books=132, group_rooms=40,
        event_covers=None, labels=("Acme Corp Annual",),
    )
    tripleseat_ish = CrmDemandDay(
        stay_date=date(2026, 8, 6), rooms_on_books=None, group_rooms=15,
        event_covers=120, labels=("Nguyen Wedding",),
    )
    assert delphi_ish.event_covers is None
    assert tripleseat_ish.rooms_on_books is None
    with pytest.raises(Exception):
        delphi_ish.rooms_on_books = 0  # frozen


def test_demand_day_refuses_negative_figures():
    """A negative room or cover count is nonsense at the boundary — loud,
    not clamped: clamping would silently rewrite provider data."""
    for field in ("rooms_on_books", "group_rooms", "event_covers"):
        with pytest.raises(ValueError):
            CrmDemandDay(stay_date=date(2026, 8, 6),
                         **{"rooms_on_books": None, "group_rooms": None,
                            "event_covers": None, field: -1})


def test_capabilities_default_to_absent():
    """An undeclared dimension is ABSENT (the G1 posture: absence degrades
    loudly, never to a silent zero)."""
    caps = CrmCapabilities()
    assert not caps.emits_rooms_on_books
    assert not caps.emits_group_rooms
    assert not caps.emits_event_covers


def test_in_memory_feed_scripts_days_and_records_calls():
    """The test fake for J4/J5: returns the scripted days (as a
    CrmDemandPull — the J3 fetch contract carries the dropped-field
    report), records every fetch (ref + window) so endpoint tests can
    assert what was asked."""
    day = CrmDemandDay(stay_date=date(2026, 8, 6), rooms_on_books=100,
                       group_rooms=None, event_covers=None)
    feed = InMemoryCrmFeed(
        days=[day],
        feed_capabilities=CrmCapabilities(emits_rooms_on_books=True),
    )
    got = feed.fetch_demand("REF-1", date(2026, 8, 3), date(2026, 8, 9))
    assert got.days == (day,)
    assert got.dropped_fields == {}
    assert feed.calls == [("REF-1", date(2026, 8, 3), date(2026, 8, 9))]
    assert feed.capabilities().emits_rooms_on_books
