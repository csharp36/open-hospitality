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
]
# Only the header area is needed; scanning a bounded prefix keeps false positives out
# of table bodies further down the page.
_HEADER_WORD_LIMIT = 120


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


def detect_report_signature(words: list[Word]) -> tuple[str, str] | None:
    """Match only the (pms_source, report_type) report signature from the header,
    WITHOUT resolving a property. Returns None if no supported signature matches.

    The anonymous preview uses this: it has no property registry, so it cannot
    call detect() (which raises unless a registered property resolves).
    """
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()
    return next((sig for phrase, sig in _REPORT_SIGNATURES if phrase in header_text), None)


def detect(words: list[Word], registry: Sequence[Mapping[str, str]]) -> Detection:
    header_text = " ".join(w.text for w in words[:_HEADER_WORD_LIMIT]).upper()

    match = detect_report_signature(words)
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
