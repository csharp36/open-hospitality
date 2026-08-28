from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from usali.adaptors.pdf import Word
from usali.models import PropertyDetectionAlias

# Report signatures: an UPPERCASED phrase that appears in the report's own text.
_REPORT_SIGNATURES: list[tuple[str, tuple[str, str]]] = [
    ("TRIAL BALANCE", ("OPERA", "trial_balance")),
    ("TRANSACTION SUMMARY", ("AUTOCLERK", "transaction_summary")),
    ("MANAGER FLASH", ("OPERA", "manager_flash")),
    ("MANAGER'S REPORT", ("AUTOCLERK", "manager_report")),
    ("MARKET CODE STATISTICS", ("OPERA", "market_stats")),
    ("RATE PLAN", ("AUTOCLERK", "rate_plan")),
    ("HOTEL JOURNAL SUMMARY", ("SKYTOUCH", "hotel_journal")),
    # Registered once the statistics adapter was recalibrated against a real
    # Standard Audit Pack. It previously matched five synthetic single-word
    # anchors emitted by the mock generator and raised on any real header, so
    # registering it would have quarantined the whole pack -- including the
    # financial section that parsed correctly. It now locates columns by SHAPE
    # (one business date, two PTD, two YTD), which holds for both header
    # variants a real export uses.
    ("HOTEL STATISTICS", ("SKYTOUCH", "hotel_statistics")),
]
# Only the header area is needed; scanning a bounded prefix keeps false positives out
# of table bodies further down the page.
_HEADER_WORD_LIMIT = 120


def supported_pms_sources() -> frozenset[str]:
    """The PMS sources with a registered ingestion pipeline, lowercased.

    Derived from `_REPORT_SIGNATURES` so there is ONE source of truth: signup
    offers exactly the sources this repo can actually detect and parse. A
    hand-maintained parallel list drifts the moment an adapter is registered (or
    un-registered), and the failure is silent -- either a source is advertised
    whose pack quarantines on ingest, or a working one is never offered.
    """
    return frozenset(source.lower() for _, (source, _) in _REPORT_SIGNATURES)


@dataclass(frozen=True)
class Detection:
    pms_source: str
    report_type: str
    property_id: str


def load_registry(session: Session) -> list[dict[str, str]]:
    """Read the property detection registry from the DB (replaces properties.yaml).

    Returns rows in the legacy YAML shape — keys `match`, `property_id`,
    `pms_source` — so `detect` is unchanged apart from taking rows, not a path.
    """
    aliases = (
        session.execute(
            select(PropertyDetectionAlias).order_by(PropertyDetectionAlias.alias_id)
        )
        .scalars()
        .all()
    )
    return [
        {
            "match": a.match_phrase,
            "property_id": a.property_id,
            "pms_source": a.pms_source,
        }
        for a in aliases
    ]


def detect_report_signature(
    words: list[Word], title: str | None = None
) -> tuple[str, str] | None:
    """Match only the (pms_source, report_type) report signature, WITHOUT
    resolving a property. Returns None if no supported signature matches.

    The anonymous preview uses this: it has no property registry, so it cannot
    call detect() (which raises unless a registered property resolves).

    `title` is a pack section's title row, which `pack.split_pack` already
    derives and groups pages by. When it is present, signatures are matched
    against the TITLE ALONE -- never the header window.

    That distinction is the whole point. A signature is a substring match, and
    the header window spans 120 words, so it reaches well past any title and
    into the table's COLUMN HEADINGS. Four unrelated SkyTouch reports print a
    `Rate Plan` column (Cancellation List, Credit Check List, No Show Report,
    Rate Discrepancy Report), so every one of them matched the bare AutoClerk
    `RATE PLAN` signature and was routed to that adapter -- either raising, or
    silently reading values out of columns that mean something else and filing
    them under the wrong PMS. A column heading is evidence about a report's
    COLUMNS; only its title is evidence about its IDENTITY.

    The trade is recall: a section whose top row is NOT its report title (a
    property banner, say) now goes unrecognised where the header window might
    have guessed it. That fails safely -- `process_pack` skips the section, and
    a pack with no recognised section is quarantined loudly -- whereas the
    behaviour it replaces attributed real figures to the wrong report type.

    A standalone single-report file is not a pack section and has no title, so
    it keeps the header-window behaviour unchanged.
    """
    if title is not None and title.strip():
        haystack = title.upper()
    else:
        haystack = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()
    return next((sig for phrase, sig in _REPORT_SIGNATURES if phrase in haystack), None)


def detect(
    words: list[Word], registry: Sequence[Mapping[str, str]], title: str | None = None
) -> Detection:
    # The PROPERTY is resolved from the header window either way: a registry
    # alias is a property name or code, which prints in the page header, not in
    # the report title. Only the report SIGNATURE moves to the title.
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()

    match = detect_report_signature(words, title)
    if match is None:
        raise ValueError("could not detect report type from PDF header")
    pms_source, report_type = match

    for row in registry:
        if row["match"].upper() in header_text:
            if row["pms_source"] != pms_source:
                raise ValueError(
                    f"property {row['property_id']} is registered for {row['pms_source']}, "
                    f"but the report looks like {pms_source}"
                )
            return Detection(
                pms_source=pms_source, report_type=report_type, property_id=row["property_id"]
            )
    raise ValueError(
        "could not resolve property from PDF header; register it with `usali seed-properties`"
    )
