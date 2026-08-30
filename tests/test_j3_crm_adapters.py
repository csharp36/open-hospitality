"""J3: the two CRM adapters through ONE allowlist filter (plan decision 5).

The adapters are the ONLY code that sees provider wire payloads, and the
allowlist is the control: exactly the mapped fields are read; every other
field — contacts above all, and every revenue/rate field — is dropped
WITHOUT being read into a variable, counted by field name in the pull's
dropped-field report. The mocks' worlds are salted with synthetic contact
and revenue fields precisely so these tests have teeth.

Property mapping: `crm_ref` is declared in `mapping/properties.yaml` and
seeded onto `Property` exactly like `wage_jurisdiction` (the registry is
the human-authored place a property is declared); those pins live in
tests/mapping/test_property_registry.py and the migration file.
"""

from datetime import date

import httpx
import pytest

from usali.crm_feed import (
    LABEL_MAX_CHARS,
    SENSITIVE_FIELD_PATTERNS,
    CrmDemandDay,
    CrmFeedError,
    InMemoryCrmFeed,
    bound_label,
    take_allowlisted,
)
from usali.delphi_adapter import DelphiAdapter
from usali.delphi_mock import DELPHI_HOTEL_REF, create_mock_delphi
from usali.qbo_client import SyncASGITransport
from usali.tripleseat_adapter import TripleseatAdapter
from usali.tripleseat_mock import TRIPLESEAT_LOCATION_ID, create_mock_tripleseat

_START = date(2026, 8, 3)
_END = date(2026, 8, 9)


def _delphi(key: str = "mock") -> DelphiAdapter:
    return DelphiAdapter(
        base_url="http://mock-delphi", subscription_key=key,
        transport=SyncASGITransport(create_mock_delphi()),
    )


def _tripleseat(key: str = "mock") -> TripleseatAdapter:
    return TripleseatAdapter(
        base_url="http://mock-tripleseat", api_key=key,
        transport=SyncASGITransport(create_mock_tripleseat()),
    )


# --- the allowlist filter itself ---------------------------------------------


def test_the_filter_never_reads_a_dropped_value():
    """"Dropped unread" is structural, not aspirational: a payload whose
    dropped values EXPLODE on access passes through the filter untouched,
    because the filter only ever indexes allowlisted keys."""

    class Tripwire(dict):
        def __getitem__(self, key):
            if key in ("Contact", "AverageRate"):
                raise AssertionError(f"read a dropped field: {key}")
            return super().__getitem__(key)

    payload = Tripwire(
        BlockName="Acme Corp Annual", Contact={"Name": "x"}, AverageRate="189",
    )
    dropped: dict[str, int] = {}
    kept = take_allowlisted(payload, frozenset({"BlockName", "StayDates"}), dropped)
    assert kept == {"BlockName": "Acme Corp Annual"}
    assert dropped == {"Contact": 1, "AverageRate": 1}


def test_the_filter_refuses_a_sensitive_allowlist():
    """The patterns list is the guard against allowlist widening: an
    allowlist entry matching a contact/revenue pattern is refused at the
    filter, so 'just add Contact to the allowlist' cannot pass review OR
    tests. Contacts and revenue are named in the patterns."""
    assert any("contact" in p for p in SENSITIVE_FIELD_PATTERNS)
    assert any(p in ("rate", "total", "revenue") for p in SENSITIVE_FIELD_PATTERNS)
    for poisoned in ("Contact", "AverageRate", "grand_total", "email_address"):
        with pytest.raises(ValueError, match="sensitive"):
            take_allowlisted({}, frozenset({poisoned, "Date"}), {})


def test_labels_are_bounded():
    long = "Nguyen Wedding " * 30
    assert len(bound_label(long)) == LABEL_MAX_CHARS
    assert bound_label("Rotary Lunch") == "Rotary Lunch"


# --- the Delphi adapter against its mock -------------------------------------


def test_delphi_normalizes_the_fixed_world():
    """Occupancy + blocks merge to one demand day per stay date. The block
    world spans TWO mock pages, so 'Coastal Runners Expo' appearing proves
    the adapter paginates. Delphi speaks pace and blocks, never covers:
    event_covers is None on every day; a day the block world does not
    mention has group_rooms 0 (blocks enumerate — no block means none
    exist), while a day the occupancy series omits stays None."""
    pull = _delphi().fetch_demand(DELPHI_HOTEL_REF, _START, _END)
    by_date = {d.stay_date: d for d in pull.days}
    assert sorted(by_date) == [date(2026, 8, n) for n in range(3, 10)]

    thursday = by_date[date(2026, 8, 6)]
    assert thursday.rooms_on_books == 132
    assert thursday.group_rooms == 40 + 10  # Acme + Delta Sigma
    assert thursday.labels == ("Acme Corp Annual", "Delta Sigma Reunion")
    assert thursday.event_covers is None

    assert by_date[date(2026, 8, 3)].group_rooms == 0
    assert by_date[date(2026, 8, 3)].labels == ()
    saturday = by_date[date(2026, 8, 8)]
    assert saturday.group_rooms == 12
    assert saturday.labels == ("Coastal Runners Expo",)  # page 2 of the mock
    assert all(d.event_covers is None for d in pull.days)


def test_delphi_drops_the_salt_unread_and_names_it():
    """Every non-allowlisted wire field lands in the dropped report BY
    NAME — the contact and rate salt above all — and nothing dropped
    appears anywhere in the normalized rows."""
    pull = _delphi().fetch_demand(DELPHI_HOTEL_REF, _START, _END)
    # BlockId is NOT here: it is allowlisted as the dedup identity (J7 —
    # a block repeated across pages must count once), the one non-figure
    # field the adapter reads beyond the mapped demand shape.
    assert pull.dropped_fields == {
        "Status": 3, "Contact": 3, "AverageRate": 3,
        "PickedUpRooms": 4,
    }
    surface = repr(pull.days)
    for leaked in ("casey", "example.test", "555-01", "189.00", "Definite"):
        assert leaked not in surface


def test_delphi_windows_the_result():
    """The mock serves its whole fixed world regardless of the requested
    dates; the ADAPTER enforces the window."""
    pull = _delphi().fetch_demand(
        DELPHI_HOTEL_REF, date(2026, 8, 6), date(2026, 8, 6)
    )
    assert [d.stay_date for d in pull.days] == [date(2026, 8, 6)]


def test_delphi_capabilities_match_what_it_emits():
    caps = _delphi().capabilities()
    assert caps.emits_rooms_on_books and caps.emits_group_rooms
    assert not caps.emits_event_covers


def test_delphi_errors_are_loud_and_never_carry_the_body():
    """A CRM error body can carry contacts and revenue; the error message
    is status + fixed text only. An unknown ref names the REF."""
    with pytest.raises(CrmFeedError) as denied:
        _delphi(key="wrong").fetch_demand(DELPHI_HOTEL_REF, _START, _END)
    assert "401" in str(denied.value)
    for fragment in ("casey", "@", "subscription key", "{"):
        assert fragment not in str(denied.value)

    with pytest.raises(CrmFeedError, match="NOPE"):
        _delphi().fetch_demand("NOPE", _START, _END)


# --- the Tripleseat adapter against its mock ---------------------------------


def test_tripleseat_normalizes_the_fixed_world():
    """Events become covers on their event date; a nested room block adds
    group rooms; Tripleseat has no pace concept, so rooms_on_books is None
    on every day (the capability gap, expressed per row)."""
    pull = _tripleseat().fetch_demand(
        str(TRIPLESEAT_LOCATION_ID), _START, _END
    )
    by_date = {d.stay_date: d for d in pull.days}
    assert sorted(by_date) == [
        date(2026, 8, 4), date(2026, 8, 6), date(2026, 8, 7),
    ]

    wedding = by_date[date(2026, 8, 6)]
    assert wedding.event_covers == 120
    assert wedding.group_rooms == 15
    assert wedding.labels == ("Nguyen Wedding",)

    lunch = by_date[date(2026, 8, 4)]
    assert lunch.event_covers == 45
    assert lunch.group_rooms == 0  # blockless event: none exist, not unknown
    assert lunch.labels == ("Rotary Lunch",)

    kickoff = by_date[date(2026, 8, 7)]
    assert kickoff.event_covers == 60 and kickoff.group_rooms == 8
    assert all(d.rooms_on_books is None for d in pull.days)


def test_tripleseat_drops_the_salt_unread_and_names_it():
    pull = _tripleseat().fetch_demand(
        str(TRIPLESEAT_LOCATION_ID), _START, _END
    )
    # `id` is NOT here: it is allowlisted as the dedup identity (J7).
    assert pull.dropped_fields == {
        "status": 3, "contact": 3, "grand_total": 3,
    }
    surface = repr(pull.days)
    for leaked in ("tam@", "555-02", "8400.00", "first_name", "definite"):
        assert leaked not in surface


def test_tripleseat_windows_the_result():
    pull = _tripleseat().fetch_demand(
        str(TRIPLESEAT_LOCATION_ID), date(2026, 8, 6), date(2026, 8, 6)
    )
    assert [d.stay_date for d in pull.days] == [date(2026, 8, 6)]


def test_tripleseat_capabilities_match_what_it_emits():
    caps = _tripleseat().capabilities()
    assert caps.emits_event_covers and caps.emits_group_rooms
    assert not caps.emits_rooms_on_books


def test_tripleseat_errors_are_loud_and_never_carry_the_body():
    with pytest.raises(CrmFeedError) as denied:
        _tripleseat(key="wrong").fetch_demand(
            str(TRIPLESEAT_LOCATION_ID), _START, _END
        )
    assert "401" in str(denied.value)
    for fragment in ("tam@", "api key", "{"):
        assert fragment not in str(denied.value)

    with pytest.raises(CrmFeedError, match="999999"):
        _tripleseat().fetch_demand("999999", _START, _END)

    # A non-numeric ref is a mapping mistake — refused by name, no HTTP.
    with pytest.raises(CrmFeedError, match="DELPHI-HISJ"):
        _tripleseat().fetch_demand("DELPHI-HISJ", _START, _END)


# --- the two adapters agree on the normalized shape --------------------------


def test_equivalent_worlds_normalize_to_the_same_rows():
    """The port abstraction is proven, not assumed: hand both adapters an
    EQUIVALENT world (one 15-room 'Nguyen Wedding' block on Aug 6) on
    their own wire shapes, and the normalized rows agree on every
    dimension both providers speak. The dimensions only one speaks come
    out None on the other — the capability gap, never a silent zero."""

    def delphi_wire(request: httpx.Request) -> httpx.Response:
        if "roomblocks" in request.url.path:
            return httpx.Response(200, json={
                "Page": 1, "TotalPages": 1, "Items": [{
                    "BlockName": "Nguyen Wedding",
                    "StayDates": [
                        {"Date": "08/06/2026", "BlockedRooms": 15},
                    ],
                }],
            })
        return httpx.Response(200, json={"Items": []})

    def tripleseat_wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": 1, "events": [{
                "name": "Nguyen Wedding", "event_date": "2026-08-06",
                "guest_count": 0,
                "room_block": {"rooms_per_night": [
                    {"date": "2026-08-06", "rooms": 15},
                ]},
            }],
        })

    delphi = DelphiAdapter(
        base_url="http://x", subscription_key="k",
        transport=httpx.MockTransport(delphi_wire),
    ).fetch_demand("REF", _START, _END)
    tripleseat = TripleseatAdapter(
        base_url="http://x", api_key="k",
        transport=httpx.MockTransport(tripleseat_wire),
    ).fetch_demand("501", _START, _END)

    assert len(delphi.days) == len(tripleseat.days) == 1
    d, t = delphi.days[0], tripleseat.days[0]
    assert (d.stay_date, d.group_rooms, d.labels) == \
        (t.stay_date, t.group_rooms, t.labels) == \
        (date(2026, 8, 6), 15, ("Nguyen Wedding",))
    # Each side's missing dimension is None exactly where the capability
    # says the provider does not speak it.
    assert d.event_covers is None and t.event_covers == 0
    assert d.rooms_on_books is None and t.rooms_on_books is None


def test_two_events_on_one_day_accumulate_covers():
    """A lunch and a dinner on the same day are BOTH demand: covers sum
    across events (the mutant that replaces instead of accumulates keeps
    only the last event and silently undercounts the kitchen's day)."""

    def wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": 1, "events": [
                {"name": "Rotary Lunch", "event_date": "2026-08-06",
                 "guest_count": 45, "room_block": None},
                {"name": "Awards Dinner", "event_date": "2026-08-06",
                 "guest_count": 60, "room_block": None},
            ],
        })

    pull = TripleseatAdapter(
        base_url="http://x", api_key="k",
        transport=httpx.MockTransport(wire),
    ).fetch_demand("501", _START, _END)
    (day,) = pull.days
    assert day.event_covers == 45 + 60
    assert day.labels == ("Rotary Lunch", "Awards Dinner")


def test_a_multi_night_block_lands_rooms_and_labels_on_every_night():
    """A wedding's room block can span nights beyond the event date. The
    extra night gets the group rooms AND the label; its covers are 0 —
    Tripleseat SPEAKS covers and no event happens that evening — never
    None (None is reserved for a dimension the provider cannot say)."""

    def wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": 1, "events": [{
                "name": "Nguyen Wedding", "event_date": "2026-08-06",
                "guest_count": 120,
                "room_block": {"rooms_per_night": [
                    {"date": "2026-08-06", "rooms": 15},
                    {"date": "2026-08-07", "rooms": 12},
                ]},
            }],
        })

    pull = TripleseatAdapter(
        base_url="http://x", api_key="k",
        transport=httpx.MockTransport(wire),
    ).fetch_demand("501", _START, _END)
    by_date = {d.stay_date: d for d in pull.days}
    night_after = by_date[date(2026, 8, 7)]
    assert night_after.group_rooms == 12
    assert night_after.event_covers == 0
    assert night_after.labels == ("Nguyen Wedding",)
    assert by_date[date(2026, 8, 6)].event_covers == 120


# --- config selection --------------------------------------------------------


def test_the_feed_is_selected_by_the_provider_name_alone():
    """L5: the feed is selected by the PROVIDER NAME the caller resolved
    (per-org, from the org's demand_feed credential row) — delphi|tripleseat
    pick an adapter, empty
    means the feature is OFF (None), and an unknown value refuses loudly.
    Base URLs stay process-wide (from_settings), so no env is needed here."""
    from usali.server import _crm_feed_for_provider

    assert isinstance(_crm_feed_for_provider("delphi"), DelphiAdapter)
    assert isinstance(_crm_feed_for_provider("tripleseat"), TripleseatAdapter)
    assert _crm_feed_for_provider("") is None
    with pytest.raises(RuntimeError, match="hubspot"):
        _crm_feed_for_provider("hubspot")


def test_the_in_memory_feed_reports_dropped_fields_too():
    """The fake carries the J3 pull shape so J4 endpoint tests can script
    a dropped-field report."""
    day = CrmDemandDay(stay_date=date(2026, 8, 6), rooms_on_books=100,
                       group_rooms=None, event_covers=None)
    feed = InMemoryCrmFeed(days=[day], dropped_fields={"Contact": 3})
    pull = feed.fetch_demand("REF-1", _START, _END)
    assert pull.days == (day,)
    assert pull.dropped_fields == {"Contact": 3}


# --- the J7 review pins ------------------------------------------------------


def test_j7_tripleseat_refuses_an_absent_figure():
    """An event that EXISTS but carries no guest_count is not zero covers
    — it is a figure the provider failed to state. Fabricating 0 would
    render "0 covers" under a capability that says Tripleseat speaks
    covers, and a GM would understaff a real event (the J7 money
    finding; Delphi's identical case already refused)."""

    def no_covers(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": 1, "events": [
                {"id": 1, "name": "Gala", "event_date": "2026-08-06",
                 "room_block": None},
            ],
        })

    with pytest.raises(CrmFeedError, match="non-integer demand figure"):
        TripleseatAdapter(
            base_url="http://x", api_key="k",
            transport=httpx.MockTransport(no_covers),
        ).fetch_demand("501", _START, _END)

    def no_rooms(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": 1, "events": [
                {"id": 1, "name": "Gala", "event_date": "2026-08-06",
                 "guest_count": 120,
                 "room_block": {"rooms_per_night": [
                     {"date": "2026-08-06"},
                 ]}},
            ],
        })

    with pytest.raises(CrmFeedError, match="non-integer demand figure"):
        TripleseatAdapter(
            base_url="http://x", api_key="k",
            transport=httpx.MockTransport(no_rooms),
        ).fetch_demand("501", _START, _END)


def test_j7_a_fractional_figure_refuses_never_truncates():
    """`int(str(raw))` is deliberate: "132.7" refuses instead of quietly
    becoming 132 (every suite figure is whole, so truncation would be
    invisible — the G7 whole-dollar lesson applied to demand)."""

    def delphi_wire(request: httpx.Request) -> httpx.Response:
        if "occupancy" in request.url.path:
            return httpx.Response(200, json={"Items": [
                {"Date": "08/06/2026", "RoomsOnTheBooks": "132.7"},
            ]})
        return httpx.Response(200, json={"TotalPages": 1, "Items": []})

    with pytest.raises(CrmFeedError, match="non-integer demand figure"):
        DelphiAdapter(
            base_url="http://x", subscription_key="k",
            transport=httpx.MockTransport(delphi_wire),
        ).fetch_demand("REF", _START, _END)

    def ts_wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": 1, "events": [
                {"id": 1, "name": "Gala", "event_date": "2026-08-06",
                 "guest_count": "45.5", "room_block": None},
            ],
        })

    with pytest.raises(CrmFeedError, match="non-integer demand figure"):
        TripleseatAdapter(
            base_url="http://x", api_key="k",
            transport=httpx.MockTransport(ts_wire),
        ).fetch_demand("501", _START, _END)


def test_j7_tripleseat_paginates():
    """Two pages, two DISTINCT events: both count. The Tripleseat mock
    world fits one page, so before this pin the pagination loop was dead
    code under test (an `if True: break` survived)."""

    def wire(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        events = {
            1: [{"id": 1, "name": "Rotary Lunch", "event_date": "2026-08-06",
                 "guest_count": 45, "room_block": None}],
            2: [{"id": 2, "name": "Awards Dinner", "event_date": "2026-08-06",
                 "guest_count": 60, "room_block": None}],
        }[page]
        return httpx.Response(200, json={
            "page": page, "total_pages": 2, "events": events,
        })

    pull = TripleseatAdapter(
        base_url="http://x", api_key="k",
        transport=httpx.MockTransport(wire),
    ).fetch_demand("501", _START, _END)
    (day,) = pull.days
    assert day.event_covers == 45 + 60


def test_j7_an_entity_repeated_across_pages_counts_once():
    """Offset pagination can serve the same entity on two pages when the
    provider's result set shifts mid-pull. The same wedding must not
    become double demand: the natural key (`id` / `BlockId`) is the
    dedup identity — the ONE non-demand field the adapters read."""

    def ts_wire(request: httpx.Request) -> httpx.Response:
        event = {"id": 7, "name": "Nguyen Wedding",
                 "event_date": "2026-08-06", "guest_count": 120,
                 "room_block": {"rooms_per_night": [
                     {"date": "2026-08-06", "rooms": 15},
                 ]}}
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={
            "page": page, "total_pages": 2, "events": [event],
        })

    pull = TripleseatAdapter(
        base_url="http://x", api_key="k",
        transport=httpx.MockTransport(ts_wire),
    ).fetch_demand("501", _START, _END)
    (day,) = pull.days
    assert day.event_covers == 120
    assert day.group_rooms == 15
    assert day.labels == ("Nguyen Wedding",)

    def delphi_wire(request: httpx.Request) -> httpx.Response:
        if "roomblocks" in request.url.path:
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json={
                "Page": page, "TotalPages": 2, "Items": [{
                    "BlockId": 7, "BlockName": "Nguyen Wedding",
                    "StayDates": [
                        {"Date": "08/06/2026", "BlockedRooms": 15},
                    ],
                }],
            })
        return httpx.Response(200, json={"Items": []})

    pull = DelphiAdapter(
        base_url="http://x", subscription_key="k",
        transport=httpx.MockTransport(delphi_wire),
    ).fetch_demand("REF", _START, _END)
    (day,) = pull.days
    assert day.group_rooms == 15
    assert day.labels == ("Nguyen Wedding",)


def test_j7_a_poisoned_page_count_refuses_loudly():
    """`TotalPages` comes off the wire OUTSIDE the allowlist; before this
    pin a non-numeric value escaped as a bare ValueError — a 500 whose
    traceback carries the wire value into the server log, skipping the
    audited-refusal path (the J7 disclosure finding). An implausibly
    large value is refused too: the pull loop must be bounded, not
    steerable into an HTTP call per claimed page."""

    def delphi_wire(request: httpx.Request) -> httpx.Response:
        if "roomblocks" in request.url.path:
            return httpx.Response(200, json={
                "TotalPages": "casey@example.test", "Items": [],
            })
        return httpx.Response(200, json={"Items": []})

    with pytest.raises(CrmFeedError) as err:
        DelphiAdapter(
            base_url="http://x", subscription_key="k",
            transport=httpx.MockTransport(delphi_wire),
        ).fetch_demand("REF", _START, _END)
    assert "page count" in str(err.value)
    assert "casey" not in str(err.value)  # the wire value is never echoed

    def ts_wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": {"nested": "junk"}, "events": [],
        })

    with pytest.raises(CrmFeedError, match="page count"):
        TripleseatAdapter(
            base_url="http://x", api_key="k",
            transport=httpx.MockTransport(ts_wire),
        ).fetch_demand("501", _START, _END)

    def bottomless(request: httpx.Request) -> httpx.Response:
        if "roomblocks" in request.url.path:
            return httpx.Response(200, json={
                "TotalPages": 10**9, "Items": [],
            })
        return httpx.Response(200, json={"Items": []})

    with pytest.raises(CrmFeedError, match="page count"):
        DelphiAdapter(
            base_url="http://x", subscription_key="k",
            transport=httpx.MockTransport(bottomless),
        ).fetch_demand("REF", _START, _END)


def test_j7_labels_are_bounded_at_the_adapter():
    """`bound_label` is applied where the wire enters, not just testable
    in isolation: a 200-char block/event name comes out at the 80-char
    bound (the J7 review found the adapter-level application had no
    direct pin — only the storage-side 300 join cap did)."""
    long_name = "Coastal " * 30  # 240 chars

    def delphi_wire(request: httpx.Request) -> httpx.Response:
        if "roomblocks" in request.url.path:
            return httpx.Response(200, json={
                "TotalPages": 1, "Items": [{
                    "BlockId": 1, "BlockName": long_name,
                    "StayDates": [
                        {"Date": "08/06/2026", "BlockedRooms": 15},
                    ],
                }],
            })
        return httpx.Response(200, json={"Items": []})

    pull = DelphiAdapter(
        base_url="http://x", subscription_key="k",
        transport=httpx.MockTransport(delphi_wire),
    ).fetch_demand("REF", _START, _END)
    (label,) = pull.days[0].labels
    assert label == long_name[:LABEL_MAX_CHARS]

    def ts_wire(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "page": 1, "total_pages": 1, "events": [
                {"id": 1, "name": long_name, "event_date": "2026-08-06",
                 "guest_count": 45, "room_block": None},
            ],
        })

    pull = TripleseatAdapter(
        base_url="http://x", api_key="k",
        transport=httpx.MockTransport(ts_wire),
    ).fetch_demand("501", _START, _END)
    (label,) = pull.days[0].labels
    assert label == long_name[:LABEL_MAX_CHARS]


@pytest.mark.parametrize("pattern", SENSITIVE_FIELD_PATTERNS)
def test_j7_every_sensitive_pattern_polices_the_allowlist(pattern):
    """One poisoned allowlist per pattern: dropping or typo'ing ANY entry
    of SENSITIVE_FIELD_PATTERNS fails here by name (the J7 review found
    only 4 of the 11 patterns were pinned)."""
    with pytest.raises(ValueError, match=pattern):
        take_allowlisted({}, frozenset({f"Wire{pattern.title()}Field"}), {})


def test_j7_a_non_object_wire_shape_refuses():
    """A string where an object belongs must refuse, not be iterated
    character-by-character into the dropped report (the J7 disclosure
    finding: attacker text walked into a character histogram)."""
    dropped: dict[str, int] = {}
    with pytest.raises(CrmFeedError, match="not an object"):
        take_allowlisted("casey@example.test", frozenset({"Date"}), dropped)
    assert dropped == {}


def test_j7_hostile_field_names_are_shaped_in_the_dropped_report():
    """The dropped report is names-only — and a NAME is only reported
    verbatim when it is shaped like a field name. A contact string used
    as a JSON key must not ride the report out through the refresh
    receipt or the demo's stdout note."""
    dropped: dict[str, int] = {}
    kept = take_allowlisted({
        "Date": "08/06/2026",
        "casey.contact@example.test / cell 555-0100": "x",
        "A" * 200: "y",
        "PickedUpRooms": 4,
    }, frozenset({"Date"}), dropped)
    assert kept == {"Date": "08/06/2026"}
    assert dropped == {"PickedUpRooms": 1, "<non-identifier field>": 2}
