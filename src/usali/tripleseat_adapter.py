"""Tripleseat-shaped CRM demand adapter (Pillar J3). Real httpx; the mock
or the real endpoint is a base_url + api-key change. Maps Tripleseat's
snake_case / ISO-date events wire shape onto the normalized demand day.

Tripleseat speaks EVENTS: guest counts (covers) on the event date, plus
an optional nested room block adding group rooms per night. It has no
rooms-on-the-books concept — `rooms_on_books` is None on every row, and
the capability flags say so (the port's None-dimension rule).

SECURITY: same posture as the Delphi adapter — every wire object routes
through `take_allowlisted`; contact and revenue fields (`contact`,
`grand_total`) are dropped without their values being bound, counted by
name in the pull report; error messages never carry a response body.
"""

from datetime import date
from typing import Any

import httpx

from usali.config import Settings
from usali.crm_feed import (
    CrmCapabilities,
    CrmDemandDay,
    CrmDemandPull,
    CrmFeedError,
    bound_label,
    take_allowlisted,
)

# The complete set of wire fields this adapter reads (plan decision 5).
# `id` is the dedup identity — offset pagination can serve the same
# event on two pages when the provider's result set shifts mid-pull, and
# a repeated event must not become double demand (J7).
_EVENT_FIELDS = frozenset({"id", "name", "event_date", "guest_count",
                           "room_block"})
_ROOM_BLOCK_FIELDS = frozenset({"rooms_per_night"})
_NIGHT_FIELDS = frozenset({"date", "rooms"})

# The pull loop is bounded: a wire-supplied page count may not steer the
# adapter into an HTTP call per claimed page (J7).
_MAX_PAGES = 500


def _iso_date(raw: object) -> date:
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise CrmFeedError(
            "tripleseat returned an unparseable event date"
        ) from exc


class TripleseatAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=15,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "TripleseatAdapter":
        return cls(base_url=settings.tripleseat_base_url,
                   api_key=settings.tripleseat_api_key)

    def capabilities(self) -> CrmCapabilities:
        return CrmCapabilities(
            emits_group_rooms=True, emits_event_covers=True,
        )

    def fetch_demand(
        self, external_ref: str, start: date, end: date
    ) -> CrmDemandPull:
        # Tripleseat identifies a property by numeric location id; a
        # non-numeric ref is a MAPPING mistake (somebody pointed a Delphi
        # ref at this adapter) — refuse by name before any HTTP.
        try:
            location_id = int(external_ref)
        except ValueError:
            raise CrmFeedError(
                f"tripleseat ref must be a numeric location id, "
                f"got {external_ref!r}"
            ) from None

        dropped: dict[str, int] = {}
        covers: dict[date, int] = {}
        group: dict[date, int] = {}
        labels: dict[date, list[str]] = {}
        seen_events: set[str | int] = set()
        page = 1
        while True:
            body = self._get(page, location_id, external_ref, start, end)
            for event in body.get("events", []):
                kept = take_allowlisted(event, _EVENT_FIELDS, dropped)
                event_id = kept.get("id")
                if isinstance(event_id, (str, int)):
                    if event_id in seen_events:
                        continue  # served again by a shifted page (J7)
                    seen_events.add(event_id)
                name = bound_label(str(kept.get("name", "")))
                touched: set[date] = set()

                day = _iso_date(kept.get("event_date"))
                if start <= day <= end:
                    # No default: an event that EXISTS but states no
                    # guest_count refuses — fabricating 0 covers for a
                    # real event is the absence-becomes-zero direction
                    # the None rule exists to prevent (J7).
                    covers[day] = (
                        covers.get(day, 0)
                        + self._figure(kept.get("guest_count"))
                    )
                    touched.add(day)

                block = kept.get("room_block")
                if block:
                    bkept = take_allowlisted(
                        block, _ROOM_BLOCK_FIELDS, dropped
                    )
                    for night in bkept.get("rooms_per_night", []):
                        nkept = take_allowlisted(night, _NIGHT_FIELDS, dropped)
                        nday = _iso_date(nkept.get("date"))
                        if not (start <= nday <= end):
                            continue
                        group[nday] = (
                            group.get(nday, 0)
                            + self._figure(nkept.get("rooms"))
                        )
                        touched.add(nday)

                if name:
                    for t in touched:
                        labels.setdefault(t, []).append(name)
            if page >= self._page_count(body.get("total_pages", 1)):
                break
            page += 1

        days = tuple(
            CrmDemandDay(
                stay_date=day,
                rooms_on_books=None,  # Tripleseat has no pace concept
                group_rooms=group.get(day, 0),
                event_covers=covers.get(day, 0),
                labels=tuple(labels.get(day, ())),
            )
            for day in sorted(set(covers) | set(group))
        )
        return CrmDemandPull(days=days, dropped_fields=dropped)

    @staticmethod
    def _figure(raw: object) -> int:
        # Via str on purpose: a fractional or absent figure refuses
        # loudly rather than truncating.
        try:
            return int(str(raw))
        except ValueError as exc:
            raise CrmFeedError(
                "tripleseat returned a non-integer demand figure"
            ) from exc

    @staticmethod
    def _page_count(raw: object) -> int:
        # The paging control comes off the wire OUTSIDE the allowlist —
        # parse it with the same posture as every other wire scalar: a
        # fixed refusal, never a bare ValueError that would carry the
        # wire value into a traceback and skip the audited-refusal
        # path (J7).
        try:
            count = int(str(raw))
        except ValueError as exc:
            raise CrmFeedError(
                "tripleseat returned a non-integer page count"
            ) from exc
        if count > _MAX_PAGES:
            raise CrmFeedError(
                f"tripleseat returned an implausible page count "
                f"(over {_MAX_PAGES})"
            )
        return count

    def _get(
        self, page: int, location_id: int, external_ref: str,
        start: date, end: date,
    ) -> dict[str, Any]:
        try:
            resp = self._http.get("/v1/events.json", params={
                "location_id": location_id,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "api_key": self._api_key,
                "page": page,
            })
        except httpx.HTTPError as exc:
            raise CrmFeedError(
                f"tripleseat unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 404:
            raise CrmFeedError(
                f"tripleseat does not know location ref {external_ref!r}"
            )
        if resp.status_code >= 400:
            # Status only, NEVER the body — CRM error bodies can echo
            # contacts and revenue.
            raise CrmFeedError(
                f"tripleseat request failed ({resp.status_code})"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CrmFeedError("tripleseat returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise CrmFeedError("tripleseat returned an unexpected body shape")
        return payload
