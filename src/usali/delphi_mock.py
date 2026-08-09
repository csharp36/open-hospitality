"""A local Amadeus-Delphi-shaped sales CRM mock (Pillar J). DEV/TEST ONLY.

Speaks the enterprise *style* the real Delphi integration surface is
expected to: PascalCase JSON in a paged envelope, US-format dates
(MM/DD/YYYY), API-management subscription-key auth. Serves a FIXED
synthetic world (a fat Thursday group block and a rooms-on-the-books
curve) — this mock is the CONTRACT for the J3 adapter until an Amadeus
sandbox exists.

The payloads are deliberately SALTED with synthetic contact and rate
fields: the adapter must drop them unread, and the J1 tests pin that the
salt is really on the wire so the J3 drop test has teeth. Synthetic data
only; request bodies are never logged (there are no bodies — read-only).
"""

from typing import Any

from fastapi import FastAPI, HTTPException, Request

DELPHI_HOTEL_REF = "DELPHI-HISJ"
_KEY = "mock"
_PAGE_SIZE = 2  # small on purpose: the adapter MUST paginate

# The fixed synthetic world. Dates are US-format strings, the wire shape.
# Salt fields (Contact, AverageRate) are synthetic and exist to be dropped.
_ROOM_BLOCKS: list[dict[str, Any]] = [
    {
        "BlockId": "BLK-1001",
        "BlockName": "Acme Corp Annual",
        "Status": "Definite",
        "StayDates": [
            {"Date": "08/06/2026", "BlockedRooms": 40, "PickedUpRooms": 25},
            {"Date": "08/07/2026", "BlockedRooms": 30, "PickedUpRooms": 18},
        ],
        "Contact": {"Name": "Casey Contact", "Email": "casey@example.test",
                    "Phone": "555-0100"},
        "AverageRate": "189.00",
    },
    {
        "BlockId": "BLK-1002",
        "BlockName": "Coastal Runners Expo",
        "Status": "Tentative",
        "StayDates": [
            {"Date": "08/08/2026", "BlockedRooms": 12, "PickedUpRooms": 4},
        ],
        "Contact": {"Name": "Robin Registrar", "Email": "robin@example.test",
                    "Phone": "555-0101"},
        "AverageRate": "159.00",
    },
    {
        "BlockId": "BLK-1003",
        "BlockName": "Delta Sigma Reunion",
        "Status": "Definite",
        "StayDates": [
            {"Date": "08/06/2026", "BlockedRooms": 10, "PickedUpRooms": 10},
        ],
        "Contact": {"Name": "Jo Organizer", "Email": "jo@example.test",
                    "Phone": "555-0102"},
        "AverageRate": "175.00",
    },
]

_ROOMS_ON_BOOKS: list[dict[str, Any]] = [
    {"Date": "08/03/2026", "RoomsOnTheBooks": 88},
    {"Date": "08/04/2026", "RoomsOnTheBooks": 92},
    {"Date": "08/05/2026", "RoomsOnTheBooks": 97},
    {"Date": "08/06/2026", "RoomsOnTheBooks": 132},  # the fat Thursday
    {"Date": "08/07/2026", "RoomsOnTheBooks": 118},
    {"Date": "08/08/2026", "RoomsOnTheBooks": 74},
    {"Date": "08/09/2026", "RoomsOnTheBooks": 61},
]


def _check_key(request: Request) -> None:
    if request.headers.get("Ocp-Apim-Subscription-Key") != _KEY:
        raise HTTPException(status_code=401,
                            detail={"error": "invalid subscription key"})


def _check_hotel(hotel_ref: str) -> None:
    if hotel_ref != DELPHI_HOTEL_REF:
        raise HTTPException(status_code=404,
                            detail={"error": "unknown hotel"})


def create_mock_delphi() -> FastAPI:
    """Build the mock Delphi FastAPI app; the world is fixed per module."""
    app = FastAPI(title="mock-delphi", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.get("/api/v2/hotels/{hotel_ref}/roomblocks")
    async def room_blocks(
        hotel_ref: str, request: Request,
        startDate: str, endDate: str, page: int = 1,  # noqa: N803
    ) -> dict[str, Any]:
        _check_key(request)
        _check_hotel(hotel_ref)
        total_pages = -(-len(_ROOM_BLOCKS) // _PAGE_SIZE)
        if page < 1 or page > total_pages:
            raise HTTPException(status_code=400,
                                detail={"error": "page out of range"})
        lo = (page - 1) * _PAGE_SIZE
        return {
            "Page": page,
            "TotalPages": total_pages,
            "Items": _ROOM_BLOCKS[lo:lo + _PAGE_SIZE],
        }

    @app.get("/api/v2/hotels/{hotel_ref}/occupancy")
    async def occupancy(
        hotel_ref: str, request: Request,
        startDate: str, endDate: str,  # noqa: N803
    ) -> dict[str, Any]:
        _check_key(request)
        _check_hotel(hotel_ref)
        return {"Items": list(_ROOMS_ON_BOOKS)}

    return app
