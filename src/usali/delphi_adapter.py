"""Delphi-shaped CRM demand adapter (Pillar J3). Real httpx; the mock or
the real endpoint is a base_url + subscription-key change. Maps Delphi's
paged PascalCase / US-date wire shape onto the normalized demand day.

Delphi speaks rooms-on-the-books (pace) and group room blocks; it has no
covers concept — `event_covers` is None on every row, and the capability
flags say so, so surfaces label the gap instead of rendering a silent 0.

SECURITY: every wire object is routed through `take_allowlisted` — the
mapped fields below are ALL this adapter ever reads; contacts and rate
fields are dropped without their values being bound, counted by name in
the pull report. Error messages carry status + fixed text only, NEVER a
response body: a CRM error body can echo contacts and revenue.

Block status (Definite vs Tentative) is deliberately NOT modeled in this
pillar: the hint surface shows gross forward demand, and a tentative
200-room block is still something a GM wants to see coming. A confidence
flag is a follow-on if the surface needs one — recorded, not smuggled in.
"""

from datetime import date, datetime
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

# The complete set of wire fields this adapter reads — nothing else is
# ever bound to a variable (plan decision 5). `BlockId` is the dedup
# identity: offset pagination can serve the same block on two pages when
# the provider's result set shifts mid-pull, and a repeated block must
# not become double demand (J7).
_BLOCK_FIELDS = frozenset({"BlockId", "BlockName", "StayDates"})
_STAY_FIELDS = frozenset({"Date", "BlockedRooms"})
_OCCUPANCY_FIELDS = frozenset({"Date", "RoomsOnTheBooks"})

# The pull loop is bounded: a wire-supplied page count may not steer the
# adapter into an HTTP call per claimed page (J7).
_MAX_PAGES = 500


def _us_date(raw: object) -> date:
    try:
        return datetime.strptime(str(raw), "%m/%d/%Y").date()
    except ValueError as exc:
        # Fixed message: the raw value came off the wire — not sensitive
        # in itself, but the posture is that nothing unparsed is echoed.
        raise CrmFeedError("delphi returned an unparseable stay date") from exc


class DelphiAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        subscription_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=15,
            headers={"Ocp-Apim-Subscription-Key": subscription_key},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "DelphiAdapter":
        return cls(base_url=settings.delphi_base_url,
                   subscription_key=settings.delphi_subscription_key)

    def capabilities(self) -> CrmCapabilities:
        return CrmCapabilities(
            emits_rooms_on_books=True, emits_group_rooms=True,
        )

    def fetch_demand(
        self, external_ref: str, start: date, end: date
    ) -> CrmDemandPull:
        dropped: dict[str, int] = {}
        window = {"startDate": start.strftime("%m/%d/%Y"),
                  "endDate": end.strftime("%m/%d/%Y")}

        # Rooms on the books: a per-date series. A date the series omits
        # stated NO figure — that day stays None, never 0.
        rooms: dict[date, int] = {}
        body = self._get(
            f"/api/v2/hotels/{external_ref}/occupancy", window, external_ref
        )
        for item in body.get("Items", []):
            kept = take_allowlisted(item, _OCCUPANCY_FIELDS, dropped)
            day = _us_date(kept.get("Date"))
            if start <= day <= end:
                rooms[day] = self._figure(kept.get("RoomsOnTheBooks"))

        # Group blocks: enumerated entities, paged (the mock's page size
        # is deliberately small — pagination is load-bearing). A day no
        # block mentions has 0 group rooms: none exist, not unknown.
        group: dict[date, int] = {}
        labels: dict[date, list[str]] = {}
        seen_blocks: set[str | int] = set()
        page = 1
        while True:
            body = self._get(
                f"/api/v2/hotels/{external_ref}/roomblocks",
                {**window, "page": page}, external_ref,
            )
            for block in body.get("Items", []):
                kept = take_allowlisted(block, _BLOCK_FIELDS, dropped)
                block_id = kept.get("BlockId")
                if isinstance(block_id, (str, int)):
                    if block_id in seen_blocks:
                        continue  # served again by a shifted page (J7)
                    seen_blocks.add(block_id)
                name = bound_label(str(kept.get("BlockName", "")))
                for stay in kept.get("StayDates", []):
                    skept = take_allowlisted(stay, _STAY_FIELDS, dropped)
                    day = _us_date(skept.get("Date"))
                    if not (start <= day <= end):
                        continue
                    group[day] = (
                        group.get(day, 0)
                        + self._figure(skept.get("BlockedRooms"))
                    )
                    if name:
                        labels.setdefault(day, []).append(name)
            if page >= self._page_count(body.get("TotalPages", 1)):
                break
            page += 1

        days = tuple(
            CrmDemandDay(
                stay_date=day,
                rooms_on_books=rooms.get(day),
                group_rooms=group.get(day, 0),
                event_covers=None,  # Delphi does not speak covers
                labels=tuple(labels.get(day, ())),
            )
            for day in sorted(set(rooms) | set(group))
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
                "delphi returned a non-integer demand figure"
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
                "delphi returned a non-integer page count"
            ) from exc
        if count > _MAX_PAGES:
            raise CrmFeedError(
                f"delphi returned an implausible page count "
                f"(over {_MAX_PAGES})"
            )
        return count

    def _get(
        self, path: str, params: dict[str, Any], external_ref: str
    ) -> dict[str, Any]:
        try:
            resp = self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise CrmFeedError(
                f"delphi unreachable: {type(exc).__name__}"
            ) from exc
        if resp.status_code == 404:
            raise CrmFeedError(f"delphi does not know hotel ref {external_ref!r}")
        if resp.status_code >= 400:
            # Status only, NEVER the body — CRM error bodies can echo
            # contacts and revenue.
            raise CrmFeedError(f"delphi request failed ({resp.status_code})")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CrmFeedError("delphi returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise CrmFeedError("delphi returned an unexpected body shape")
        return payload
