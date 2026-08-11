"""Generate a pre-classified draft mapping YAML from the raw Opera transaction
code catalog.

This is a bootstrap tool: it turns the ~478-code Opera XML export into a
starting-point YAML file grouped by Opera's own TC_GROUP taxonomy, with every
row marked ``confidence: LOW`` and ``review_status: needs-review``. A human
curator reviews and promotes rows into the hand-curated ``mapping/opera.yaml``
dictionary (a separate artifact) -- this draft is never loaded directly into
the database.
"""

from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element

import yaml
from defusedxml import ElementTree as defused_et


_MAX_XML_BYTES = 5 * 1024 * 1024

# Opera TC_GROUP -> (schedule_id, major, sub). schedule_id None = non-P&L.
GROUP_MAP: dict[str, tuple[int | None, str, str]] = {
    "ROOM": (1, "Operated Departments", "Rooms"),
    "LEISURE": (3, "Operated Departments", "Other Operated Departments"),
    "TELEPHONE": (6, "Undistributed", "Information & Telecommunications Systems"),
    "MISC": (4, "Miscellaneous Income", "Miscellaneous"),
    "TAX": (None, "Taxes (Pass-Through)", "Taxes"),
    "PAY": (None, "Settlements", "Payments"),
    "LRPAY": (None, "Settlements", "Loyalty"),
    "LRADJ": (None, "Settlements", "Loyalty Adjustments"),
    "STATISTICS": (None, "Statistics", "Statistics"),
    "PETTY": (None, "Non-Operating", "Petty Cash"),
    "NONREVENUE": (None, "Non-Operating", "Non Revenue"),
    "STAY": (None, "Statistics", "Stay"),
    "PKG": (1, "Operated Departments", "Packages"),
    "ZZZ": (None, "Unmapped", "Placeholder"),
}


def _text(el: Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def generate_draft(xml_path: str | Path, out_path: str | Path, *, edition: int = 12) -> int:
    """Parse the Opera transaction code catalog XML and write a draft mapping YAML.

    Deduplicates by TRX_CODE (the catalog can list a code more than once),
    keeping the first occurrence. Every row is emitted with
    ``confidence: LOW`` and ``review_status: needs-review`` since this is an
    unreviewed, mechanically-generated draft.

    Returns the number of unique rows written.
    """
    xml_bytes = Path(xml_path).read_bytes()
    if len(xml_bytes) > _MAX_XML_BYTES:
        raise ValueError(f"Opera catalog XML exceeds {_MAX_XML_BYTES} bytes")
    # defusedxml rejects DTD/entity constructs before they can expand or read
    # external resources. Parsing the already-bounded bytes also prevents the
    # parser from consuming an arbitrarily large local export.
    root = defused_et.fromstring(xml_bytes)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for g in root.iter("G_TRX_CODE"):
        code = _text(g, "TRX_CODE")
        if not code or code in seen:
            continue
        seen.add(code)
        group = _text(g, "TC_GROUP")
        desc = _text(g, "D3")
        sched, major, sub = GROUP_MAP.get(group, (None, "Unmapped", group or "Unknown"))
        rows.append(
            {
                "source": "OPERA",
                "code": code,
                "edition": edition,
                "schedule_id": sched,
                "major": major,
                "sub": sub,
                "line_item": desc or code,
                "confidence": "LOW",
                "review_status": "needs-review",
                "notes": f"auto-draft from group {group}",
            }
        )
    Path(out_path).write_text(yaml.safe_dump(rows, sort_keys=False))
    return len(rows)
