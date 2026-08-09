"""A local Tripleseat-shaped events CRM mock (Pillar J). DEV/TEST ONLY.

Speaks the public-REST *style* the real Tripleseat API does: snake_case
JSON, ISO dates, an api_key query parameter. Serves a FIXED synthetic
world of EVENTS — guest counts (covers) and an optional nested room
block; no rooms-on-the-books anywhere, because Tripleseat has no pace
concept (the capability gap the port's None dimension expresses). This
mock is the CONTRACT for the J3 adapter until real credentials exist.

The payloads are deliberately SALTED with synthetic contact and revenue
fields (contact, grand_total): the adapter must drop them unread, and
the J1 tests pin the salt onto the wire so the J3 drop test has teeth.
Synthetic data only.
"""

from typing import Any

from fastapi import FastAPI, HTTPException, Request

TRIPLESEAT_LOCATION_ID = 501
_KEY = "mock"

# The fixed synthetic world. ISO dates, snake_case — the wire shape.
_EVENTS: list[dict[str, Any]] = [
    {
        "id": 9001,
        "name": "Nguyen Wedding",
        "status": "definite",
        "event_date": "2026-08-06",
        "guest_count": 120,
        "room_block": {
            "rooms_per_night": [{"date": "2026-08-06", "rooms": 15}],
        },
        "contact": {"first_name": "Tam", "last_name": "Nguyen",
                    "email_address": "tam@example.test",
                    "phone_number": "555-0200"},
        "grand_total": "8400.00",
    },
    {
        "id": 9002,
        "name": "Rotary Lunch",
        "status": "definite",
        "event_date": "2026-08-04",
        "guest_count": 45,
        "room_block": None,
        "contact": {"first_name": "Pat", "last_name": "President",
                    "email_address": "pat@example.test",
                    "phone_number": "555-0201"},
        "grand_total": "1350.00",
    },
    {
        "id": 9003,
        "name": "Quarterly Sales Kickoff",
        "status": "tentative",
        "event_date": "2026-08-07",
        "guest_count": 60,
        "room_block": {
            "rooms_per_night": [{"date": "2026-08-07", "rooms": 8}],
        },
        "contact": {"first_name": "Drew", "last_name": "Director",
                    "email_address": "drew@example.test",
                    "phone_number": "555-0202"},
        "grand_total": "4200.00",
    },
]


def create_mock_tripleseat() -> FastAPI:
    """Build the mock Tripleseat FastAPI app; the world is fixed."""
    app = FastAPI(title="mock-tripleseat", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.get("/v1/events.json")
    async def events(
        request: Request, location_id: int,
        start_date: str, end_date: str, api_key: str = "", page: int = 1,
    ) -> dict[str, Any]:
        if api_key != _KEY:
            raise HTTPException(status_code=401,
                                detail={"error": "invalid api key"})
        if location_id != TRIPLESEAT_LOCATION_ID:
            raise HTTPException(status_code=404,
                                detail={"error": "unknown location"})
        return {"page": page, "total_pages": 1, "events": list(_EVENTS)}

    return app
