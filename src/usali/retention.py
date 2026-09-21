"""What a filed ingest artifact keeps. Design D3-D6:
docs/design/2026-09-21-ingestion-boundary-redaction-design.md.

RETENTION is keyed exactly like ingestion._PIPELINES and pinned to it by
tests/test_retention.py::test_every_pipeline_has_a_retention_policy.
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

from usali.adaptors.hotelkey_ar_aging import _AGING_COLUMNS
from usali.adaptors.pdf import Word
from usali.adaptors.xlsx import XLSX_COL_STEP, XLSX_ROW_STEP
from usali.redaction import RedactionStats, redact_words


@dataclass(frozen=True)
class Policy:
    keep_columns: tuple[str, ...] | None = None  # XLSX allowlist; None = keep every word
    keep_first_column: bool = False  # also keep each section header's own first column
    drop_user_cells: bool = False  # title-block cells beginning "User:" are dropped


KEEP_ALL = Policy()

RETENTION: dict[tuple[str, str], Policy] = {
    ("OPERA", "trial_balance"): KEEP_ALL,
    ("OPERA", "manager_flash"): KEEP_ALL,
    ("OPERA", "market_stats"): KEEP_ALL,
    ("AUTOCLERK", "transaction_summary"): KEEP_ALL,
    ("AUTOCLERK", "manager_report"): KEEP_ALL,
    ("AUTOCLERK", "rate_plan"): KEEP_ALL,
    ("SKYTOUCH", "hotel_journal"): KEEP_ALL,
    ("SKYTOUCH", "hotel_statistics"): KEEP_ALL,
    ("HOTELKEY", "hotel_statistics"): KEEP_ALL,
    # "Count" is not in the design's D5.2 list and is not a detail column; it is
    # the Summary block's, and parse_settlement reads it to check the footing, so
    # dropping it would file an artifact the adapter refuses
    # (tests/test_retention.py::test_settlement_artifact_round_trips_through_the_adapter).
    ("HOTELKEY", "settlement"): Policy(drop_user_cells=True, keep_columns=(
        "Account Category", "Date", "Time", "Transaction Number", "Folio Number",
        "Room Number", "Payment Type", "Payment Description", "Amount", "Count",
    )),
    ("HOTELKEY", "all_payments"): Policy(
        drop_user_cells=True, keep_columns=("Payment Type", "Amount")
    ),
    # The label column's account names are what the adapter stages (design
    # section 1, last paragraph); the AR slice owns that decision. Its name
    # differs per section, so it is kept positionally rather than by name.
    ("HOTELKEY", "ar_aging"): Policy(
        drop_user_cells=True, keep_first_column=True, keep_columns=_AGING_COLUMNS
    ),
}

_XLSX_HEADER_DROP_PREFIXES = ("User:",)
_ORDINAL_COLUMN = 1  # column A: the row ordinal split_report classifies rows by
_CAPTION_COLUMN = 2  # column B: a row holding only this cell opens a section


@dataclass
class RetainedSection:
    title: str
    pms_source: str
    report_type: str
    property_id: str
    business_date: date
    words: list[Word] = field(default_factory=list)


def _apply_columns(
    words: list[Word], keep: tuple[str, ...], *, keep_first_column: bool
) -> tuple[list[Word], int]:
    """Filter an XLSX export's cells to a per-section column allowlist (design D5.2).

    A HotelKey export is a stack of sections, each with its own header row;
    ``adaptors/hotelkey_xlsx.split_report`` is where that shape is read back.
    A caption row — one cell, in column B — opens a section, and the first row
    under it naming at least two kept columns is that section's header; from
    there down, a cell is kept when its column is one of that header's kept
    columns. Column A is always kept: it holds the ordinal ``split_report``
    classifies a row by (tests/test_retention.py::
    test_settlement_artifact_round_trips_through_the_adapter). With
    ``keep_first_column`` the header's own first column is kept as well, for a
    report whose label column is named differently in every section.

    Rows above the first caption are the title block and pass through, as does
    a section in which no row names two kept columns. A kept column the export
    does not have is simply absent from that header
    (test_a_kept_column_the_export_lacks_does_not_fail_retention); nothing here
    refuses an export, because this runs after the adapter has accepted one.
    """
    rows: dict[int, list[tuple[int, int]]] = defaultdict(list)  # row -> [(column, index)]
    for i, w in enumerate(words):
        rows[round(w.top / XLSX_ROW_STEP)].append((round(w.x0 / XLSX_COL_STEP), i))

    dropped_indices: set[int] = set()
    keep_cols: set[int] | None = None
    in_section = False
    for row_idx in sorted(rows):
        cells = sorted(rows[row_idx])
        if [column for column, _ in cells] == [_CAPTION_COLUMN]:
            in_section, keep_cols = True, None
            continue
        if not in_section:
            continue
        if keep_cols is None:
            named = [column for column, i in cells if words[i].text in keep]
            if len(named) < 2:
                continue  # not this section's header row yet
            keep_cols = set(named) | {_ORDINAL_COLUMN}
            if keep_first_column:
                keep_cols.add(cells[0][0])
        dropped_indices.update(i for column, i in cells if column not in keep_cols)

    kept = [w for i, w in enumerate(words) if i not in dropped_indices]
    return kept, len(dropped_indices)


def _drop_header_cells(words: list[Word]) -> tuple[list[Word], int]:
    """Drop XLSX title-block cells naming the exporting operator (design D5.2,
    last sentence): any cell whose text starts with a prefix in
    ``_XLSX_HEADER_DROP_PREFIXES``."""
    kept = [w for w in words if not w.text.startswith(_XLSX_HEADER_DROP_PREFIXES)]
    return kept, len(words) - len(kept)


def retain_section(section: RetainedSection) -> tuple[RetainedSection, RedactionStats]:
    policy = RETENTION[(section.pms_source, section.report_type)]
    words = section.words
    cells_dropped = 0
    header_cells_dropped = 0
    if policy.keep_columns is not None:
        words, cells_dropped = _apply_columns(
            words, policy.keep_columns, keep_first_column=policy.keep_first_column
        )
    if policy.drop_user_cells:
        words, header_cells_dropped = _drop_header_cells(words)
    words, pan_stats = redact_words(words)
    stats = RedactionStats(
        pans_masked=pan_stats.pans_masked,
        cells_dropped=cells_dropped,
        header_cells_dropped=header_cells_dropped,
    )
    return replace(section, words=words), stats


def _received_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_artifact(
    *,
    source_file: str,
    data: bytes,
    kind: str,
    sections: list[RetainedSection],
    sections_dropped: list[str],
) -> dict[str, object]:
    total = RedactionStats()
    section_dicts: list[dict[str, object]] = []
    for section in sections:
        retained, stats = retain_section(section)
        total = RedactionStats(
            pans_masked=total.pans_masked + stats.pans_masked,
            cells_dropped=total.cells_dropped + stats.cells_dropped,
            header_cells_dropped=total.header_cells_dropped + stats.header_cells_dropped,
        )
        section_dicts.append(
            {
                "title": retained.title,
                "pms_source": retained.pms_source,
                "report_type": retained.report_type,
                "property_id": retained.property_id,
                "business_date": retained.business_date.isoformat(),
                "words": [{"text": w.text, "x0": w.x0, "top": w.top} for w in retained.words],
            }
        )
    return {
        "source_file": source_file,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "received_at": _received_at(),
        "kind": kind,
        "sections_dropped": list(sections_dropped),
        "redaction": {
            "pans_masked": total.pans_masked,
            "cells_dropped": total.cells_dropped,
            "header_cells_dropped": total.header_cells_dropped,
        },
        "sections": section_dicts,
    }


def write_artifact(processed_dir: Path, artifact: dict[str, object]) -> Path:
    stem = Path(str(artifact["source_file"])).stem or "upload"
    sha8 = str(artifact["sha256"])[:8]
    processed_dir.mkdir(parents=True, exist_ok=True)
    path = processed_dir / f"{stem}.{sha8}.redacted.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2))
    return path


def write_error_record(
    failed_dir: Path, *, source_file: str, data: bytes, error: str
) -> Path:
    stem = Path(source_file).stem or "upload"
    sha256 = hashlib.sha256(data).hexdigest()
    record: dict[str, object] = {
        "source_file": source_file,
        "sha256": sha256,
        "bytes": len(data),
        "received_at": _received_at(),
        "error": error[:500],
    }
    failed_dir.mkdir(parents=True, exist_ok=True)
    path = failed_dir / f"{stem}.{sha256[:8]}.error.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return path
